#!/usr/bin/env python3
"""
Snow Plow Bot (template)

Monitors PlogNorge (Mobiwin) winter-maintenance data for a configurable area
and reports the plow status of road segments near a configurable address to
Bluesky. Everything location-specific lives in config.toml.

Run modes:
  - With BSKY_HANDLE + BSKY_PASSWORD set: posts to Bluesky via atproto.
  - Without credentials (or with --dry-run): prints the would-be post.
  - --list-groups: prints the status groups available for the area.
"""

from __future__ import annotations

import argparse
import io
import logging
import math
import os
import re
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from config import Config, load_config

load_dotenv()

BSKY_HANDLE = os.getenv("BSKY_HANDLE")
BSKY_PASSWORD = os.getenv("BSKY_PASSWORD")

# Used when the status group can't be fetched (PlogNorge's usual defaults).
DEFAULT_STATUS_GROUP = {
    "Id": None,
    "Name": "",
    "ColorHexNotVisited": "#ff0000",
    "ColorHexVisisted": "#3333ff",
    "ColorHexRecentlyVisited": "#33ff00",
    "MinutesBackRecentlyVisited": 240,
    "MinutesBackVisited": 720,
}

NORWEGIAN_MONTHS = [
    "Januar", "Februar", "Mars", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Desember",
]
NORWEGIAN_WEEKDAYS = [
    "Mandag", "Tirsdag", "Onsdag", "Torsdag", "Fredag", "Lørdag", "Søndag",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


# ---------------------------------------------------------------------------
# PlogNorge data
# ---------------------------------------------------------------------------
def fetch_status_session(cfg: Config) -> requests.Session:
    """GET the area page, capture the ASP.NET anti-forgery token + cookies.

    The server remembers the area in the session, so later API calls
    (GetStatusGroups, GetVisits) apply to the configured area.
    """
    page = cfg.map_url()
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Referer": page,
        }
    )
    r = s.get(page, timeout=30, allow_redirects=False)
    if r.status_code != 200:
        raise RuntimeError(
            f"PlogNorge area '{cfg.area}' not found ({page} -> HTTP {r.status_code})"
        )
    m = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r.text)
    if not m:
        raise RuntimeError("No __RequestVerificationToken found in page")
    s.headers["RequestVerificationToken"] = m.group(1)
    s.headers["X-Requested-With"] = "XMLHttpRequest"
    return s


def get_status_groups(cfg: Config, s: requests.Session | None = None) -> list[dict]:
    s = s or fetch_status_session(cfg)
    r = s.get(f"{cfg.base_url}/Home/GetStatusGroups", timeout=30)
    r.raise_for_status()
    return r.json() or []


def resolve_status_group(cfg: Config, groups: list[dict]) -> dict:
    """Pick the configured status group (or the first one if none is set)."""
    if not groups:
        raise RuntimeError(f"Area '{cfg.area}' has no status groups")
    if cfg.status_group_id is None:
        return groups[0]
    for g in groups:
        if g.get("Id") == cfg.status_group_id:
            return g
    available = ", ".join(f"{g['Id']} ({g['Name']})" for g in groups)
    raise RuntimeError(
        f"status_group_id {cfg.status_group_id} not found for area "
        f"'{cfg.area}'. Available: {available}"
    )


def get_plognorge_data(cfg: Config) -> tuple[dict, list[dict]]:
    """Return (status_group, segments) for the configured area."""
    s = fetch_status_session(cfg)
    group = resolve_status_group(cfg, get_status_groups(cfg, s))
    pr = s.post(
        f"{cfg.base_url}/Home/GetVisits",
        json={
            "Date": datetime.now(timezone.utc).isoformat(),
            "StatusGroupId": group["Id"],
        },
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=120,
    )
    pr.raise_for_status()
    return group, pr.json().get("MaintenanceObjectVisits") or []


def parse_wkt(wkt: str) -> tuple[str, list[list[tuple[float, float]]]]:
    """Parse WKT into (kind, parts) where each part is [(lng, lat), ...].

    Supports (MULTI)POINT, (MULTI)LINESTRING and (MULTI)POLYGON.
    """
    m = re.match(r"\s*([A-Z]+)", wkt or "")
    kind = m.group(1) if m else ""
    parts = []
    for group in re.findall(r"\(([^()]+)\)", wkt or ""):
        pts = [
            (float(a), float(b))
            for a, b in re.findall(r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)", group)
        ]
        if pts:
            parts.append(pts)
    return kind, parts


def _to_local_m(lat0: float, lng0: float, lng: float, lat: float):
    """Equirectangular projection to metres around (lat0, lng0)."""
    R = 6371000.0
    x = math.radians(lng - lng0) * R * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * R
    return x, y


def _point_in_ring(x: float, y: float, ring: list[tuple[float, float]]) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def distance_to_geometry_m(cfg: Config, kind: str, parts) -> float:
    """Min distance (m) from the configured location to a parsed geometry."""
    best = math.inf
    for part in parts:
        pts = [_to_local_m(cfg.latitude, cfg.longitude, lng, lat) for lng, lat in part]
        if kind.endswith("POLYGON") and len(pts) > 2 and _point_in_ring(0, 0, pts):
            return 0.0
        if len(pts) == 1:
            best = min(best, math.hypot(*pts[0]))
            continue
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            dx, dy = x2 - x1, y2 - y1
            seg_len2 = dx * dx + dy * dy
            t = 0.0 if seg_len2 == 0 else max(0.0, min(1.0, -(x1 * dx + y1 * dy) / seg_len2))
            best = min(best, math.hypot(x1 + t * dx, y1 + t * dy))
    return best


def parse_visit_time(cfg: Config, raw):
    """PlogNorge LatestVisitTime is naive local time, e.g. '2026-11-13T21:53:41.14'."""
    if not raw:
        return None
    try:
        naive = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return naive.replace(tzinfo=cfg.tz)


def segments_near_target(cfg: Config, all_segments: list[dict]) -> list[dict]:
    """Return segments whose geometry passes within radius_m of the location."""
    out = []
    for seg in all_segments:
        kind, parts = parse_wkt(seg.get("Wkt") or "")
        if not parts:
            continue
        dm = distance_to_geometry_m(cfg, kind, parts)
        if cfg.min_distance_m <= dm <= cfg.radius_m:
            out.append(
                {
                    "id": seg.get("Id"),
                    "color": seg.get("Color"),
                    "last_visit_raw": seg.get("LatestVisitTime"),
                    "last_visit": parse_visit_time(cfg, seg.get("LatestVisitTime")),
                    "distance_m": round(dm, 1),
                    "kind": kind,
                    "parts": parts,
                }
            )
    out.sort(key=lambda x: x["distance_m"])
    return out


def get_latest_event_time(cfg: Config, segments: list[dict]) -> str:
    """'Siste hendelse: Fredag 13 November 2026 kl. 07:37' (local time)."""
    visits = [s["last_visit"] for s in segments if s.get("last_visit")]
    if not visits:
        return "Siste hendelse: Ingen data"
    t = max(visits).astimezone(cfg.tz)
    return (
        f"Siste hendelse: {NORWEGIAN_WEEKDAYS[t.weekday()]} {t.day} "
        f"{NORWEGIAN_MONTHS[t.month - 1]} {t.year} kl. {t.hour:02d}:{t.minute:02d}"
    )


# ---------------------------------------------------------------------------
# Post composition
# ---------------------------------------------------------------------------
def group_hours(group: dict) -> tuple[float, float]:
    """(recently_visited_hours, visited_hours) thresholds of a status group."""
    return (
        group.get("MinutesBackRecentlyVisited", 240) / 60,
        group.get("MinutesBackVisited", 720) / 60,
    )


def status_label(seg: dict, group: dict) -> str:
    lv = seg["last_visit"]
    if lv is None:
        return "no recorded plow visit"
    hours = (datetime.now(timezone.utc) - lv).total_seconds() / 3600
    recent_h, visited_h = group_hours(group)
    if hours <= recent_h:
        return f"plowed (visited < {recent_h:g}h)"
    if hours <= visited_h:
        return f"plowed (visited < {visited_h:g}h)"
    return f"NOT plowed recently (last plow {int(hours)}h ago)"


def build_post_text(cfg: Config, near: list[dict], group: dict) -> str:
    """Compose the Bluesky post for the current plow status (max ~300 chars)."""
    now_local = datetime.now(cfg.tz)
    lines = [
        f"🚜 Plog-status {cfg.name} ({now_local:%Y-%m-%d %H:%M}):",
        f"• {len(near)} segmenter < {cfg.radius_m:g} m",
    ]
    visits = [s["last_visit"].astimezone(cfg.tz) for s in near if s["last_visit"]]
    if visits:
        lines += [
            f"• Sist pløyd: {max(visits):%d.%m.%Y kl %H.%M}",
            f"• Eldste: {min(visits):%d.%m.%Y kl %H.%M}",
        ]
    else:
        lines.append("• Ingen pløyebesøk registrert")

    lines.append(f"Kart: {cfg.map_url(group.get('Id'))}")
    if cfg.hashtags:
        lines.append(" ".join(f"#{h}" for h in cfg.hashtags))
    return "\n".join(lines)


def build_alt_text(cfg: Config, near: list[dict], group: dict) -> str:
    recent_h, visited_h = group_hours(group)
    return (
        f"Snow-plow status map around {cfg.name}, showing {len(near)} road "
        f"segments within {cfg.radius_m:g} m coloured by PlogNorge status "
        f"(plowed within {recent_h:g} h, within {visited_h:g} h, or not "
        f"plowed recently)."
    )


def dry_run(text: str) -> bool:
    print("\n" + "=" * 60)
    print("DRY RUN - would post to Bluesky:")
    print("=" * 60)
    print(text)
    print("=" * 60)
    return True


def encode_image_for_bluesky(image_path: str) -> bytes:
    """Re-encode a map image as JPEG under Bluesky's practical size limit."""
    from PIL import Image

    im = Image.open(image_path).convert("RGB")
    if max(im.size) > 2048:
        im.thumbnail((2048, 2048))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def post_to_bluesky(text: str, image_path: str | None = None,
                    alt_text: str = "") -> bool:
    if not BSKY_HANDLE or not BSKY_PASSWORD:
        logging.warning("BSKY_HANDLE/BSKY_PASSWORD not set - skipping Bluesky post")
        return False
    from atproto import Client, models  # lazy: keep dry runs dependency-free

    client = Client()
    client.login(BSKY_HANDLE.strip(), BSKY_PASSWORD.strip())

    if not image_path:
        client.send_post(text)
        logging.info("Posted text to Bluesky as %s", BSKY_HANDLE)
        return True

    blob = client.upload_blob(encode_image_for_bluesky(image_path)).blob
    client.app.bsky.feed.post.create(
        repo=client.me.did,
        record=models.AppBskyFeedPost.Record(
            text=text,
            created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            embed=models.AppBskyEmbedImages.Main(
                images=[models.AppBskyEmbedImages.Image(image=blob, alt=alt_text)]
            ),
        ),
    )
    logging.info("Posted to Bluesky (with map image) as %s", BSKY_HANDLE)
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def list_groups(cfg: Config) -> int:
    groups = get_status_groups(cfg)
    print(f"Status groups for PlogNorge area '{cfg.area}':")
    for g in groups:
        recent_h, visited_h = group_hours(g)
        print(
            f"  {g['Id']:>5}  {g['Name']}  "
            f"(recent < {recent_h:g} h, visited < {visited_h:g} h)  "
            f"{cfg.map_url(g['Id'])}"
        )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("--config", help="Path to config file (default: config.toml)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the post instead of sending it")
    ap.add_argument("--no-post", action="store_true",
                    help="Fetch + print, never send (alias of --dry-run)")
    ap.add_argument("--list-groups", action="store_true",
                    help="List the status groups for the configured area and exit")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
    except (OSError, ValueError, KeyError) as e:
        logging.error("Invalid configuration: %s", e)
        return 2

    if args.list_groups:
        return list_groups(cfg)

    logging.info("Fetching PlogNorge data for area '%s' ...", cfg.area)
    try:
        group, all_segments = get_plognorge_data(cfg)
    except Exception as e:
        logging.error("Failed to fetch PlogNorge data: %s", e)
        return 1

    logging.info("Got %d segments total (status group %s: %s)",
                 len(all_segments), group.get("Id"), group.get("Name"))
    near = segments_near_target(cfg, all_segments)
    if not near:
        logging.warning("No segments found within %g m of %s", cfg.radius_m, cfg.name)
    for s in near:
        logging.info("  seg %s  %6.1f m  %s", s["id"], s["distance_m"], status_label(s, group))

    text = build_post_text(cfg, near, group)

    # Best-effort map image: falls back to a vector schematic without tiles.
    image_path = None
    try:
        from render_map import render_map
        image_path = render_map(cfg, near, group)
    except Exception as e:
        logging.warning("Map image render failed, posting text only: %s", e)

    if args.dry_run or args.no_post:
        ok = dry_run(text)
        if image_path:
            logging.info("Map image saved to %s (dry run)", image_path)
    else:
        alt_text = build_alt_text(cfg, near, group)
        ok = post_to_bluesky(text, image_path, alt_text) or dry_run(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
