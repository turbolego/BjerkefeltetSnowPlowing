#!/usr/bin/env python3
"""
Bjerkefeltet Snow Plow Bot
Monitors PlogNorge (Mobiwin) winter-maintenance data for Bærum and reports
the plow status of road segments near Bjerkehaugen 6 (Lommedalen, Bærum)
to Bluesky.

Data source:
  https://mobiwinqa.bmsystem.se/PlogNorge/baerum?groupId=80
  (status group 80 = "Vintervedlikehold kommunalt veinett", Bærum)

Run modes:
  - With BSKY_HANDLE + BSKY_PASSWORD set: posts to Bluesky via atproto.
  - Without credentials (or with --dry-run): prints the would-be post
    to stdout instead of posting.
"""

import argparse
import logging
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PLOGNOR_BASE = "https://mobiwinqa.bmsystem.se/PlogNorge"
STATUS_GROUP_ID = 80            # Bærum winter maintenance ("baerum")
CITY = "baerum"

# Bjerkehaugen 6 (Lommedalen, Bærum) - kommune 3201-100/152
TARGET = {"lat": 59.9636663, "lng": 10.4678833}
TARGET_NAME = "Bjerkehaugen 6, Lommedalen (Bærum)"
TARGET_RADIUS_M = 150          # report segments whose geometry comes this close
MIN_DISTANCE_M = 5            # ignore segments whose closest approach is tiny noise

# PlogNorge "visited" thresholds (from Home/GetStatusGroups):
#   recently visited = last 4 h, visited = last 12 h, older = not visited
RECENTLY_VISITED_HOURS = 4
VISITED_HOURS = 12

# Treat PlogNorge timestamps (naive) as Europe/Oslo local time.
OSLO_UTC_OFFSETS = {
    3: 2, 4: 2, 5: 2, 6: 2, 9: 1, 10: 1, 11: 1, 12: 1,
    1: 1, 2: 1, 7: 1, 8: 1,
}

BSKY_HANDLE = os.getenv("BSKY_HANDLE")
BSKY_PASSWORD = os.getenv("BSKY_PASSWORD")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


# ---------------------------------------------------------------------------
# PlogNorge data
# ---------------------------------------------------------------------------
def fetch_status_session() -> requests.Session:
    """GET the map page, capture the ASP.NET anti-forgery token + session cookies."""
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Referer": f"{PLOGNOR_BASE}/{CITY}?groupId={STATUS_GROUP_ID}",
        }
    )
    r = s.get(f"{PLOGNOR_BASE}/{CITY}?groupId={STATUS_GROUP_ID}", timeout=30)
    r.raise_for_status()
    m = re.search(
        r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r.text
    )
    if not m:
        raise RuntimeError("No __RequestVerificationToken found in page")
    s.headers["RequestVerificationToken"] = m.group(1)
    s.headers["X-Requested-With"] = "XMLHttpRequest"
    return s


def get_plognorge_segments() -> list[dict]:
    """Fetch all road/maintenance segments for Bærum winter maintenance."""
    s = fetch_status_session()
    payload = {
        "Date": datetime.now(timezone.utc).isoformat(),
        "StatusGroupId": STATUS_GROUP_ID,
    }
    pr = s.post(
        f"{PLOGNOR_BASE}/Home/GetVisits",
        json=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=120,
    )
    pr.raise_for_status()
    return pr.json().get("MaintenanceObjectVisits") or []


def parse_wkt(wkt: str) -> list[tuple[float, float]]:
    """Parse 'LINESTRING (lng lat, ...)' -> [(lng, lat), ...]."""
    pairs = re.findall(r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)", wkt)
    return [(float(lng), float(lat)) for lng, lat in pairs]


def haversine_m(lat1, lng1, lat2, lng2) -> float:
    """Distance in metres between two lat/lng points."""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def min_dist_to_segment_m(pts: list[tuple[float, float]]) -> float:
    """Min distance (metres) from TARGET to any vertex of the segment."""
    return min(
        haversine_m(TARGET["lat"], TARGET["lng"], lat, lng)
        for lng, lat in pts
    )


def parse_visit_time(raw):
    """PlogNorge LatestVisitTime is naive Oslo-local, e.g. '2026-11-13T21:53:41.14'."""
    if not raw:
        return None
    try:
        naive = datetime.fromisoformat(raw)
    except ValueError:
        return None
    offset = OSLO_UTC_OFFSETS.get(naive.month, 1)
    return naive.replace(tzinfo=timezone(timedelta(hours=offset)))


def segments_near_target(all_segments: list[dict]) -> list[dict]:
    """Return segments whose geometry passes within TARGET_RADIUS_M of the address."""
    out = []
    for seg in all_segments:
        wkt = seg.get("Wkt") or ""
        if not re.search(r"LINESTRING|POINT", wkt):
            continue
        pts = parse_wkt(wkt)
        if not pts:
            continue
        dm = min_dist_to_segment_m(pts)
        if MIN_DISTANCE_M <= dm <= TARGET_RADIUS_M:
            out.append(
                {
                    "id": seg.get("Id"),
                    "color": seg.get("Color"),
                    "last_visit_raw": seg.get("LatestVisitTime"),
                    "last_visit": parse_visit_time(seg.get("LatestVisitTime")),
                    "distance_m": round(dm, 1),
                }
            )
    out.sort(key=lambda x: x["distance_m"])
    return out


# ---------------------------------------------------------------------------
# Post composition
# ---------------------------------------------------------------------------
def status_label(seg: dict) -> str:
    lv = seg["last_visit"]
    if lv is None:
        return "no recorded plow visit"
    age = datetime.now(timezone.utc) - lv
    hours = age.total_seconds() / 3600
    if hours <= RECENTLY_VISITED_HOURS:
        return "plowed (visited < 4h)"
    if hours <= VISITED_HOURS:
        return "plowed (visited < 12h)"
    return f"NOT plowed recently (last plow {int(hours)}h ago)"


def format_days_since(dt) -> str:
    hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    if hours < 1:
        return "under en time"
    if hours < 48:
        return f"{hours:.0f} timer"
    days = hours / 24
    return f"{round(days, 1)} dager".replace(".", ",")


def build_post_text(near: list[dict]) -> str:
    """Compose the Bluesky post for the current plow status."""
    now_local = datetime.now(timezone.utc).astimezone()
    with_visit = [s for s in near if s["last_visit"] is not None]
    never = len(near) - len(with_visit)

    if with_visit:
        last = max(s["last_visit"] for s in with_visit)
        oldest = min(s["last_visit"] for s in with_visit)
        lines = [
            f"🚜 Plog-status for {TARGET_NAME} ({now_local:%Y-%m-%d %H:%M} CET):",
            "",
            f"• {len(near)} road segments within {TARGET_RADIUS_M} m",
            f"• Most recent plow: {last:%d.%m.%Y kl %H.%M} (for {format_days_since(last)} since)",
            f"• Oldest plow in area: {oldest:%d.%m.%Y kl %H.%M}",
        ]
        if never:
            lines.append(f"• {never} segment(s) with no recorded plow visit at all")
    else:
        lines = [
            f"🚜 Plog-status for {TARGET_NAME} ({now_local:%Y-%m-%d %H:%M} CET):",
            "",
            f"• {len(near)} road segments within {TARGET_RADIUS_M} m",
            "• No plow visits recorded for any of them",
        ]

    lines += [
        "",
        f"Kart: {PLOGNOR_BASE.replace('mobiwinqa', 'mobiwinqa')}/{CITY}?groupId={STATUS_GROUP_ID}",
        "",
        "#plogging #bærum #bjerkefeltet #snø #wintermaintenance",
    ]
    return "\n".join(lines)


def dry_run(text: str) -> bool:
    print("\n" + "=" * 60)
    print("DRY RUN - would post to Bluesky:")
    print("=" * 60)
    print(text)
    print("=" * 60)
    return True


def post_to_bluesky(text: str) -> bool:
    if not BSKY_HANDLE or not BSKY_PASSWORD:
        logging.warning(
            "BSKY_HANDLE/BSKY_PASSWORD not set - skipping Bluesky post"
        )
        return False
    from atproto import Client  # lazy: keep dry runs dependency-free

    client = Client()
    client.login(BSKY_HANDLE.strip(), BSKY_PASSWORD.strip())
    client.send_post(text)
    logging.info("Posted to Bluesky as %s", BSKY_HANDLE)
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the post instead of sending it")
    ap.add_argument("--no-post", action="store_true",
                    help="Fetch + print, never send (alias of --dry-run)")
    args = ap.parse_args()

    logging.info("Fetching PlogNorge Bærum winter-maintenance data ...")
    try:
        all_segments = get_plognorge_segments()
    except Exception as e:
        logging.error("Failed to fetch PlogNorge data: %s", e)
        return 1

    logging.info("Got %d segments total", len(all_segments))
    near = segments_near_target(all_segments)
    if not near:
        logging.warning("No segments found within %d m of %s",
                        TARGET_RADIUS_M, TARGET_NAME)

    for s in near:
        logging.info(
            "  seg %s  %6.1f m  %s", s["id"], s["distance_m"], status_label(s)
        )

    text = build_post_text(near)
    if args.dry_run or args.no_post:
        ok = dry_run(text)
    else:
        ok = post_to_bluesky(text) or dry_run(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())