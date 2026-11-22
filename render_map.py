#!/usr/bin/env python3
"""render_map.py - render a snow-plow status map centred on the configured location."""

from __future__ import annotations

import io
import math
import statistics as st
import time

import requests
from PIL import Image, ImageDraw, ImageFont

from config import Config, load_config

TILE = 256
FONT_SIZE = 15
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/arial.ttf",
    "DejaVuSans.ttf",
    "Arial.ttf",
]


def lonlat_to_world_px(lng: float, lat: float, zoom: int) -> tuple[float, float]:
    """Web-mercator global pixel coordinates at the given zoom level."""
    scale = TILE * 2 ** zoom
    x = (lng + 180.0) / 360.0 * scale
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * scale
    return x, y


def metres_per_px(lat: float, zoom: int) -> float:
    return 156543.03392 * math.cos(math.radians(lat)) / 2 ** zoom


def resolve_zoom(cfg: Config) -> int:
    """Configured zoom, or the closest zoom whose view still fits radius_m."""
    if cfg.map_zoom != "auto":
        return int(cfg.map_zoom)
    half_view_px = cfg.map_size_px / 2
    for z in range(19, 0, -1):
        if cfg.radius_m * 1.1 <= half_view_px * metres_per_px(cfg.latitude, z):
            return z
    return 1


def load_font(cfg: Config, size: int = FONT_SIZE):
    for path in ([cfg.font] if cfg.font else []) + FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def is_real_tile(data: bytes) -> bool:
    """Reject tiny/blank/placeholder tiles (e.g. OSM's '418 blocked' tile)."""
    if len(data) < 1000:
        return False
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        if im.size != (TILE, TILE):
            return False
        pix = list(im.getdata())
        return st.pvariance([sum(p) // 3 for p in pix]) > 100
    except Exception:
        return False


def fetch_base_map(cfg: Config, zoom: int, origin: tuple[float, float]):
    """Stitch the XYZ tiles covering the image; None if no tile could be loaded."""
    size = cfg.map_size_px
    ox, oy = origin
    n = 2 ** zoom
    base = Image.new("RGB", (size, size), (240, 238, 232))
    headers = {"User-Agent": "SnowPlowBot/1.0 (+https://github.com/turbolego/BjerkefeltetSnowPlowing)"}
    loaded = 0
    for ty in range(int(oy // TILE), int((oy + size) // TILE) + 1):
        if not 0 <= ty < n:
            continue
        for tx in range(int(ox // TILE), int((ox + size) // TILE) + 1):
            url = cfg.tile_url.format(z=zoom, x=tx % n, y=ty)
            try:
                r = requests.get(url, headers=headers, timeout=15)
                ok = r.status_code == 200 and is_real_tile(r.content)
            except requests.RequestException:
                ok = False
            if ok:
                tile = Image.open(io.BytesIO(r.content)).convert("RGB")
                base.paste(tile, (int(tx * TILE - ox), int(ty * TILE - oy)))
                loaded += 1
            else:
                print(f"  skip tile {zoom}/{tx}/{ty}")
            time.sleep(0.2)  # be polite to the tile server
    print(f"  loaded {loaded} tiles at zoom {zoom}")
    return base if loaded else None


def vector_base(size: int) -> Image.Image:
    img = Image.new("RGB", (size, size), (235, 233, 225))
    d = ImageDraw.Draw(img)
    for v in range(0, size, 30):
        d.line([(v, 0), (v, size)], fill=(215, 213, 205), width=1)
        d.line([(0, v), (size, v)], fill=(215, 213, 205), width=1)
    return img


def draw_box(d: ImageDraw.ImageDraw, box):
    d.rectangle(box, fill=(255, 255, 255), outline=(20, 20, 20), width=2)


def render_map(cfg: Config, segments: list[dict], group: dict,
               out_path: str | None = None) -> str:
    from plowbot import get_latest_event_time, group_hours

    out_path = out_path or cfg.map_output
    size = cfg.map_size_px
    zoom = resolve_zoom(cfg)
    cx, cy = lonlat_to_world_px(cfg.longitude, cfg.latitude, zoom)
    origin = (cx - size / 2, cy - size / 2)

    base = fetch_base_map(cfg, zoom, origin)
    if base is None:
        print("  map tiles unavailable - using vector fallback")
        base = vector_base(size)
    d = ImageDraw.Draw(base)

    def to_img(lng, lat):
        x, y = lonlat_to_world_px(lng, lat, zoom)
        return x - origin[0], y - origin[1]

    for seg in sorted(segments, key=lambda s: -s["distance_m"]):
        color = seg.get("color") or group.get("ColorHexNotVisited", "#ff0000")
        for part in seg["parts"]:
            pts = [to_img(lng, lat) for lng, lat in part]
            if seg["kind"].endswith("POLYGON") and len(pts) > 2:
                d.line(pts + pts[:1], fill=color, width=3, joint="curve")
            elif len(pts) > 1:
                d.line(pts, fill=color, width=3, joint="curve")
            else:
                x, y = pts[0]
                d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=color)

    # Address marker (map is centred on it)
    mx, my = size / 2, size / 2
    d.ellipse([mx - 4, my - 4, mx + 4, my + 4], fill=(20, 20, 20))

    font = load_font(cfg)
    pad = 12

    # Top-left: latest event time
    title = get_latest_event_time(cfg, segments)
    tb = d.textbbox((0, 0), title, font=font)
    box = [pad, pad, pad + (tb[2] - tb[0]) + pad * 2, pad + (tb[3] - tb[1]) + pad * 2]
    draw_box(d, box)
    d.text(((box[0] + box[2]) // 2, (box[1] + box[3]) // 2), title,
           font=font, anchor="mm", fill=(10, 10, 10))

    # Top-right: legend using the status group's own colours/thresholds
    recent_h, visited_h = group_hours(group)
    legend = [
        (group.get("ColorHexNotVisited", "#ff0000"), f"Ikke pløyd nylig (>{visited_h:g}t)"),
        (group.get("ColorHexVisisted", "#3333ff"), f"Pløyd siste {visited_h:g}t"),
        (group.get("ColorHexRecentlyVisited", "#33ff00"), f"Pløyd siste {recent_h:g}t"),
    ]
    max_label_w = max(d.textbbox((0, 0), lb, font=font)[2] for _, lb in legend)
    swatch_w, swatch_gap, row_h, line_gap = 16, 8, 22, 4
    content_w = swatch_w + swatch_gap + max_label_w
    content_h = len(legend) * row_h + (len(legend) - 1) * line_gap
    bx1, by0 = size - pad, pad
    bx0, by1 = bx1 - (content_w + pad * 2), by0 + content_h + pad * 2
    draw_box(d, [bx0, by0, bx1, by1])
    for i, (clr, lb) in enumerate(legend):
        row_mid = by0 + pad + i * (row_h + line_gap) + row_h // 2
        d.line([(bx0 + pad, row_mid), (bx0 + pad + swatch_w, row_mid)], fill=clr, width=5)
        d.text((bx0 + pad + swatch_w + swatch_gap, row_mid), lb,
               font=font, anchor="lm", fill=(20, 20, 20))

    # Bottom-right: tile attribution
    if cfg.attribution:
        small = load_font(cfg, 11)
        ab = d.textbbox((0, 0), cfg.attribution, font=small)
        aw, ah = ab[2] - ab[0], ab[3] - ab[1]
        d.rectangle([size - aw - 8, size - ah - 8, size, size], fill=(255, 255, 255))
        d.text((size - 4, size - 4), cfg.attribution, font=small, anchor="rd", fill=(60, 60, 60))

    base.save(out_path)
    print(f"  saved {out_path} ({size}x{size}, zoom {zoom})")
    return out_path


if __name__ == "__main__":
    from plowbot import get_plognorge_data, segments_near_target

    cfg = load_config()
    group, all_segments = get_plognorge_data(cfg)
    segs = segments_near_target(cfg, all_segments)
    print(f"{len(segs)} segments within {cfg.radius_m:g} m")
    for s in segs:
        print(f"  id {s['id']} {s['distance_m']:.0f} m {s['color']}")
    render_map(cfg, segs, group)
