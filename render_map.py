#!/usr/bin/env python3
"""
render_map.py - render a snow-plow status map image for Bjerkehaugen 6.

Given the PlogNorge segments near the target address, produce an annotated
map image (OSM raster base if tiles are reachable, otherwise a clean vector
schematic fallback) with the segments drawn in PlogNorge status colours, a
150 m radius ring, the address marker, a title/subtitle, a legend and
attribution.  Used by plowbot.py to attach an image to the Bluesky post.
"""

import io, math, re
from datetime import datetime, timezone
import requests
from PIL import Image, ImageDraw, ImageFont

TARGET = {"lat": 59.9636663, "lng": 10.4678833}
TARGET_NAME = "Bjerkehaugen 6, Lommedalen"
RADIUS_M = 150
ZOOM = 17
HALF_SPAN_M = 700          # half-width of the map extent around the target
TILE = 256
def image_size():
    """Final image size = native tile-grid size (square tiles -> square image)."""
    x_min, x_max, y_min, y_max = tile_range()
    return (x_max - x_min + 1) * TILE, (y_max - y_min + 1) * TILE


STATUS_COLORS = {
    "#ff0000": ("red",   (226, 28, 28)),
    "#3333ff": ("blue",  (48, 110, 230)),
    "#33ff00": ("green", (40, 190, 70)),
}
OSLO_UTC_OFFSETS = {1:1,2:1,3:2,4:2,5:2,6:2,7:1,8:1,9:1,10:1,11:1,12:1}

# ----------------------------------------------------------------------------
# geo helpers
# ----------------------------------------------------------------------------
def lonlat_to_mercator_m(lng, lat):
    x = math.radians(lng) * 6378137.0
    y = math.log(math.tan(math.pi/4 + math.radians(lat)/2)) * 6378137.0
    return x, y

def mercator_m_to_lonlat(x, y):
    lng = math.degrees(x / 6378137.0)
    lat = math.degrees(2 * math.atan(math.exp(y / 6378137.0)) - math.pi/2)
    return lng, lat

def tile_range():
    """Return (x_min, x_max, y_min, y_max) tiles covering TARGET +/- HALF_SPAN_M.

    Uses the standard Web-Mercator tile-index formula (offset by world_m/2
    since EPSG:3857 x is 0 at the prime meridian)."""
    cx, cy = lonlat_to_mercator_m(TARGET["lng"], TARGET["lat"])
    n = 2 ** ZOOM
    world_m = 2 * math.pi * 6378137.0
    tile_m = world_m / n
    tx_center = int((cx + world_m / 2) / tile_m)
    ty_center = int((world_m / 2 - cy) / tile_m)
    span = int(math.ceil(HALF_SPAN_M / tile_m))
    x_min = max(0, tx_center - span)
    x_max = min(n - 1, tx_center + span)
    y_min = max(0, ty_center - span)
    y_max = min(n - 1, ty_center + span)
    return x_min, x_max, y_min, y_max

def image_geometry():
    """Return (px_per_m, x0_m, y0_m) for the target tile grid, where
    (x0_m, y0_m) is the EPSG:3857 top-left corner of the grid."""
    x_min, x_max, y_min, y_max = tile_range()
    n = 2 ** ZOOM
    world_m = 2 * math.pi * 6378137.0
    tile_m = world_m / n
    px_per_m = TILE / tile_m
    x0_m = x_min * tile_m - world_m / 2
    y0_m = world_m / 2 - y_min * tile_m
    return px_per_m, x0_m, y0_m

# base map
# ----------------------------------------------------------------------------
def _tile_color_buckets(content: bytes) -> int:
    """Number of distinct 32-level color buckets in a tile (0 on decode failure)."""
    try:
        im = Image.open(io.BytesIO(content)).convert("RGB")
        if im.size[0] < 256:
            return 0
        px = im.load()
        seen = set()
        for y in range(0, im.size[1], 2):
            for x in range(0, im.size[0], 2):
                r, g, b = px[x, y]
                seen.add((r // 32, g // 32, b // 32))
        return len(seen)
    except Exception:
        return 0

def _is_real_tile(content: bytes) -> bool:
    """A real OSM street tile has many distinct colours.  Placeholders and
    OSM's 'Access blocked' error tiles have very few (< 40)."""
    return len(content) > 500 and content[:4] == b"\x89PNG" and _tile_color_buckets(content) >= 40

def fetch_base_map() -> Image.Image | None:
    """Download OSM tiles with spacing; return the composited base or None."""
    x_min, x_max, y_min, y_max = tile_range()
    tw, th = x_max - x_min + 1, y_max - y_min + 1
    img = Image.new("RGB", (tw * TILE, th * TILE), (232, 234, 236))
    sess = requests.Session()
    sess.headers["User-Agent"] = "Mozilla/5.0"
    ok = 0
    for ty in range(y_min, y_max + 1):
        for tx in range(x_min, x_max + 1):
            try:
                r = sess.get(f"https://tile.openstreetmap.org/{ZOOM}/{tx}/{ty}.png", timeout=15)
                if r.status_code == 200 and _is_real_tile(r.content):
                    tile = Image.open(io.BytesIO(r.content)).convert("RGB")
                    img.paste(tile, ((tx - x_min) * TILE, (ty - y_min) * TILE))
                    ok += 1
            except Exception:
                pass
            import time
            time.sleep(0.4)   # stay within OSM tile usage policy
    print(f"  base map: {ok}/{tw*th} tiles loaded")
    # If the majority of tiles are placeholders/blocked, treat it as a failure
    # so the caller falls back to the clean vector schematic instead of a
    # patchwork of error tiles.
    return img if ok >= (tw * th) // 2 else None

def vector_base() -> Image.Image:
    """Clean schematic base if OSM tiles are unavailable."""
    x_min, x_max, y_min, y_max = tile_range()
    img = Image.new("RGB", ((x_max-x_min+1)*TILE, (y_max-y_min+1)*TILE), (250, 251, 252))
    d = ImageDraw.Draw(img)
    W, H = img.size
    # light grid
    for x in range(0, W, TILE//2):
        d.line([(x,0),(x,H)], fill=(228,230,233), width=1)
    for y in range(0, H, TILE//2):
        d.line([(0,y),(W,y)], fill=(228,230,233), width=1)
    d.rectangle([0,0,W-1,H-1], outline=(190,195,200), width=3)
    return img

# ----------------------------------------------------------------------------
# text helpers
# ----------------------------------------------------------------------------
def _font(size, bold=True):
    cands = ([
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    ] if bold else []) + [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ]
    for p in cands:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()

def parse_time(raw):
    """Accept either a raw ISO string or an already-parsed datetime (plowbot format)."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    naive = datetime.fromisoformat(raw)
    off = OSLO_UTC_OFFSETS.get(naive.month, 1)
    from datetime import timedelta
    return naive.replace(tzinfo=timezone(timedelta(hours=off)))

# ----------------------------------------------------------------------------
# main render
# ----------------------------------------------------------------------------
def render_map(segments, out_path="plowmap.png", title=None, subtitle=None):
    """Render the annotated map image. `segments` is a list of dicts with
    keys: id, color, last_visit, d, pts [(lng,lat)...].  Returns (path, ok)."""
    base = fetch_base_map()
    if base is None:
        print("  OSM tiles unavailable - using vector fallback")
        base = vector_base()
    px_per_m, x0_m, y0_m = image_geometry()   # px_per_m at native tile-grid scale
    IMG_W, IMG_H = base.size

    d = ImageDraw.Draw(base)

    # target pixel
    tx_m, ty_m = lonlat_to_mercator_m(TARGET["lng"], TARGET["lat"])
    tx_px = (tx_m - x0_m) * px_per_m
    ty_px = (y0_m - ty_m) * px_per_m
    radius_px = RADIUS_M * px_per_m

    # segments (draw farthest first so near ones are on top)
    for seg in sorted(segments, key=lambda s: -s["d"]):
        key = STATUS_COLORS.get(seg.get("color"), STATUS_COLORS["#ff0000"])
        color = key[1]
        coords = []
        for lng, lat in seg["pts"]:
            mx, my = lonlat_to_mercator_m(lng, lat)
            coords.append(((mx - x0_m) * px_per_m, (y0_m - my) * px_per_m))
        if len(coords) > 1:
            d.line(coords, fill=color, width=9, joint="curve")
            d.line(coords, fill=(255,255,255), width=3, joint="curve")

    # 150 m ring + center marker
    d.ellipse([tx_px-radius_px, ty_px-radius_px, tx_px+radius_px, ty_px+radius_px],
              outline=(255,255,255), width=6)
    d.ellipse([tx_px-radius_px-3, ty_px-radius_px-3, tx_px+radius_px+3, ty_px+radius_px+3],
              outline=(20,20,20), width=2)
    d.ellipse([tx_px-13, ty_px-13, tx_px+13, ty_px+13],
              fill=(255,255,255), outline=(20,20,20), width=4)

    # ---- title band
    ft, fs, fx = _font(46), _font(28), _font(23)
    with_vis = [s for s in segments if s.get("last_visit")]
    if title is None:
        title = "Plog-status: " + TARGET_NAME
    if subtitle is None:
        if with_vis:
            last = max(parse_time(s["last_visit"]) for s in with_vis)
            age = (datetime.now(timezone.utc) - last).total_seconds() / 3600
            age_s = f"{age:.0f} timer" if age < 48 else f"{round(age/24,1):g} dager".replace(".", ",")
            subtitle = (f"{len(segments)} veistrekker innenfor {RADIUS_M} m \u2014 sist plogget "
                        f"{last:%d.%m} kl {last:%H.%M} ({age_s} siden)")
        else:
            subtitle = f"{len(segments)} veistrekker innenfor {RADIUS_M} m \u2014 ingen registrerte ploggingsturer"
    pad = 16
    b1 = d.textbbox((0,0), title, font=ft); tw_, th_ = b1[2]-b1[0], b1[3]-b1[1]
    b2 = d.textbbox((0,0), subtitle, font=fs); sw, sh = b2[2]-b2[0], b2[3]-b2[1]
    bw = max(tw_, sw) + pad*2; bh = th_ + sh + pad*3
    d.rectangle([14,14,14+bw,14+bh], fill=(255,255,255), outline=(20,20,20), width=3)
    d.text((14+pad, 14+pad), title, font=ft, fill=(10,10,10))
    d.text((14+pad, 14+pad+th_+8), subtitle, font=fs, fill=(40,40,40))

    # ---- legend
    leg = [
        ((226,28,28), "Ikke plogget nylig (> 12 t)"),
        ((48,110,230), "Plogget siste 12 timer"),
        ((40,190,70), "Plogget siste 4 timer"),
        ("ring", "Hvit sirkel = 150 m sone rundt adressen"),
    ]
    lw = 330; lh = len(leg)*34 + 24
    lx, ly = 14, IMG_H - lh - 14
    d.rectangle([lx-8, ly-8, lx-8+lw, ly-8+lh], fill=(255,255,255), outline=(20,20,20), width=3)
    for i,(col,label) in enumerate(leg):
        yy = ly+8+i*34
        if col == "ring":
            d.ellipse([lx, yy, lx+22, yy+22], outline=(255,255,255), width=5)
            d.ellipse([lx, yy, lx+22, yy+22], outline=(226,28,28), width=1)
        else:
            d.line([lx, yy+11, lx+22, yy+11], fill=col, width=6)
        d.text((lx+32, yy+1), label, font=fx, fill=(20,20,20))

    # ---- attribution
    attr = "\u00a9 OpenStreetMap-contributentene \u00b7 PlogNorge / Mobiwin"
    ba = d.textbbox((0,0), attr, font=fx); aw = ba[2]-ba[0]
    d.rectangle([IMG_W-aw-28, IMG_H-42, IMG_W-14, IMG_H-14], fill=(255,255,255), outline=(20,20,20), width=2)
    d.text((IMG_W-aw-22, IMG_H-36), attr, font=fx, fill=(70,70,70))

    base.save(out_path)
    print(f"  saved {out_path} ({IMG_W}x{IMG_H})")
    return out_path

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        # segments passed via a JSON file if desired
        import json
        segs = json.load(open(sys.argv[1]))
        render_map(segs, sys.argv[2] if len(sys.argv) > 2 else "plowmap.png")
