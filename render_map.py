#!/usr/bin/env python3
"""render_map.py - render a snow-plow status map for Bjerkehaugen 6 (200 m radius, 600x600 px)."""

import io, math, re, time
from datetime import datetime, timezone
import requests
import statistics as st
from PIL import Image, ImageDraw, ImageFont

TARGET = {"lat": 59.9636663, "lng": 10.4678833}
TARGET_NAME = "Bjerkehaugen 6, Lommedalen"
RADIUS_M = 200         # radius around address for segment filtering
ZOOM = 17
IMG_W = IMG_H = 600    # fixed output size

STATUS_COLORS = {
    "#ff0000": ("red",   (226, 28, 28)),
    "#3333ff": ("blue",  (48, 110, 230)),
    "#33ff00": ("green", (40, 190, 70)),
}
OSLO_UTC_OFFSETS = {1:1,2:1,3:2,4:2,5:2,6:2,7:1,8:1,9:1,10:1,11:1,12:1}

def lonlat_to_mercator_m(lng, lat):
    x = math.radians(lng) * 6378137.0
    y = math.log(math.tan(math.pi/4 + math.radians(lat)/2)) * 6378137.0
    return x, y

def mercator_to_pixel(mx, my, img_w, img_h, target_mx=None, target_my=None):
    """Convert mercator meters to pixel position in the 3x3 tile grid image.

    Uses exact tile-grid geometry matching fetch_base_map() to guarantee
    segments align precisely with the background tiles.
    """
    if target_mx is None or target_my is None:
        target_mx, target_my = lonlat_to_mercator_m(TARGET["lng"], TARGET["lat"])

    n = 2 ** ZOOM
    world = 2 * math.pi * 6378137.0
    tile_size = world / n
    raw_size = 3 * 256  # 768px raw grid
    scale = img_w / raw_size  # 600/768 = 0.78125

    # Grid origin = top-left of tile (x_t-1, y_t-1), matching fetch_base_map
    x_t = int((target_mx + world/2) / tile_size)
    y_t = int((world/2 - target_my) / tile_size)
    x_origin = x_t - 1
    y_origin = y_t - 1

    # Fractional position within the 3x3 grid
    frac_x = (mx + world/2) / tile_size - x_origin
    frac_y = (world/2 - my) / tile_size - y_origin

    # Convert to pixels: raw grid position * scale
    px_x = int(frac_x * 256 * scale)
    py_y = int(frac_y * 256 * scale)
    return px_x, py_y

def haversine_m(lat1, lng1, lat2, lng2):
    R=6371000.0; p1,p2=math.radians(lat1),math.radians(lat2)
    dp,dl=math.radians(lat2-lat1),math.radians(lng2-lng1)
    h=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(h))

def parse_wkt(wkt):
    return [(float(a),float(b)) for a,b in re.findall(r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)", wkt)]

def fetch_segments():
    s=requests.Session(); s.headers.update({"User-Agent":"Mozilla/5.0"})
    # 1. GET landing page to establish session / cookies
    r=s.get("https://mobiwinqa.bmsystem.se/PlogNorge/baerum?groupId=80",timeout=30); r.raise_for_status()
    # 2. POST GetVisits (the AJAX call that returns the segment table)
    pr=s.post("https://mobiwinqa.bmsystem.se/PlogNorge/Home/GetVisits",
              json={"groupId":80,"take":200,"skip":0},
              headers={"Content-Type":"application/json","X-Requested-With":"XMLHttpRequest"},
              timeout=30)
    pr.raise_for_status()
    return pr.json().get("Data",[])

def is_real_tile(data):
    if len(data) < 1000:
        return False
    from PIL import Image
    try:
        im=Image.open(io.BytesIO(data))
        if im.size!=(256,256): return False
        if im.mode == 'P':
            im = im.convert('RGB')
        pix=list(im.getdata())
        if len(pix) < 100: return False
        # pixel variance heuristic: blocked/placeholder tiles are near-uniform
        # OpenTopoMap tiles have more uniform areas, so use lower threshold
        import statistics as st
        var=st.pvariance([sum(p)//3 for p in pix])
        return var > 100
    except Exception:
        return False

def fetch_base_map():
    # Tile server (OpenTopoMap - works from cloud)
    sources=[
        f"https://tile.opentopomap.org/{ZOOM}"
    ]
    tiles=[]
    t_merc_x, t_merc_y = lonlat_to_mercator_m(TARGET["lng"], TARGET["lat"])
    n = 2 ** ZOOM
    world = 2 * math.pi * 6378137.0
    tile_size = world / n
    x_t = int((t_merc_x + world/2) / tile_size)
    y_t = int((world/2 - t_merc_y) / tile_size)
    print(f"  tile center [{x_t},{y_t}] for Bjerkehaugen 6")
    for src_url in sources:
        if len(tiles)>=9: break
        for dy in (-1,0,1):
            for dx in (-1,0,1):
                if len(tiles)>=9: break
                url=f"{src_url}/{x_t+dx}/{y_t+dy}.png"
                r=requests.get(url,headers={"User-Agent":"Mozilla/5.0"},timeout=15)
                if r.status_code==200 and is_real_tile(r.content):
                    tiles.append(Image.open(io.BytesIO(r.content)).convert("RGB"))
                    print(f"  tile OK [{x_t+dx},{y_t+dy}]")
                else:
                    print(f"  skip [{x_t+dx},{y_t+dy}] len={len(r.content)}")
                time.sleep(0.2)
    if not tiles: return None

    # Build 3x3 grid with TARGET tile in center (position [1,1])
    # This ensures consistent alignment between base map and overlays
    raw_w = 3 * 256
    raw_h = 3 * 256
    base=Image.new("RGB",(raw_w,raw_h),(240,238,232))
    for i,tile in enumerate(tiles):
        tx=i%3; ty=i//3
        base.paste(tile,(tx*256,ty*256))

    # Resize to final output size
    base=base.resize((IMG_W,IMG_H),Image.LANCZOS)
    return base

def vector_base():
    # OSM-style stylized background: pale beige/grey base, subtle grid
    img=Image.new("RGB",(IMG_W,IMG_H),(235,233,225))
    d=ImageDraw.Draw(img)
    for x in range(0, IMG_W, 30):
        d.line([(x,0),(x,IMG_H)], fill=(215,213,205), width=1)
    for y in range(0, IMG_H, 30):
        d.line([(0,y),(IMG_W,y)], fill=(215,213,205), width=1)
    return img

def render_map(segments, out_path="plowmap.png"):
    base=fetch_base_map()
    if base is None:
        print("  OSM tiles unavailable - using vector fallback")
        base=vector_base()
    d=ImageDraw.Draw(base)

    # Calculate target pixel position in the resized image
    t_merc_x, t_merc_y = lonlat_to_mercator_m(TARGET["lng"], TARGET["lat"])
    cx, cy = mercator_to_pixel(t_merc_x, t_merc_y, IMG_W, IMG_H, t_merc_x, t_merc_y)
    print(f"  target maps to pixel ({cx},{cy}) in {IMG_W}x{IMG_H} image")

    # Draw segments using consistent coordinate system
    for seg in sorted(segments,key=lambda s:-s["d"]):
        key=STATUS_COLORS.get(seg.get("color"),STATUS_COLORS["#ff0000"]); color=key[1]
        pts=[]
        for lng,la in seg["pts"]:
            mx,my = lonlat_to_mercator_m(lng,la)
            sx,sy = mercator_to_pixel(mx, my, IMG_W, IMG_H, t_merc_x, t_merc_y)
            pts.append((sx,sy))
        if len(pts)>1:
            d.line(pts, fill=color, width=3, joint="curve")

    # Small marker dot at target (address location)
    d.ellipse([cx-4,cy-4,cx+4,cy+4],fill=(20,20,20))

    # Title/subtitle - show only latest event time
    from plowbot import get_latest_event_time
    title = get_latest_event_time(segments)
    # Use DejaVuSans with 30% larger font (15pt vs default ~11.5pt)
    ft=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 15)
    fs=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 15)
    tb=d.textbbox((0,0),title,font=ft); tw,th=tb[2]-tb[0],tb[3]-tb[1]
    pad=12
    box_x0,box_y0=pad,pad
    box_x1=pad+tw+pad*2
    box_y1=pad+th+pad*2
    d.rectangle([box_x0,box_y0,box_x1,box_y1],fill=(255,255,255),outline=(20,20,20),width=2)
    # Center text horizontally and vertically
    cx_box=(box_x0+box_x1)//2
    cy_box=(box_y0+box_y1)//2
    d.text((cx_box,cy_box),title,font=ft,anchor="mm",fill=(10,10,10))

    # Legend (in a box matching the top-left style: white bg, black border) — TOP RIGHT
    leg=[((226,28,28),"Ikke pløyd nylig (>12t)"),
         ((48,110,230),"Pløyd siste 12t"),
         ((40,190,70),"Pløyd siste 4t")]
    leg_font=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 15)
    leg_widths=[d.textbbox((0,0),lb,font=leg_font)[2] for (clr,lb) in leg]
    max_label_w=max(leg_widths)
    swatch_w=16; swatch_gap=8; pad2=12
    row_h=22; line_gap=4
    content_w=swatch_w+swatch_gap+max_label_w
    content_h=len(leg)*row_h+(len(leg)-1)*line_gap
    bx1=IMG_W-pad
    by0=pad
    bx0=bx1-(content_w+pad2*2)
    by1=by0+content_h+pad2*2
    d.rectangle([bx0,by0,bx1,by1],fill=(255,255,255),outline=(20,20,20),width=2)
    for i,(clr,lb) in enumerate(leg):
        row_top=by0+pad2+i*(row_h+line_gap)
        row_mid=row_top+row_h//2
        d.line([(bx0+pad2,row_mid),(bx0+pad2+swatch_w,row_mid)],fill=clr,width=5)
        d.text((bx0+pad2+swatch_w+swatch_gap,row_mid),lb,font=leg_font,anchor="lm",fill=(20,20,20))

    base.save(out_path)
    print(f"  saved {out_path} ({IMG_W}x{IMG_H})")
    return out_path

if __name__=="__main__":
    from plowbot import get_plognorge_segments, segments_near_target
    segs=segments_near_target(get_plognorge_segments())
    print(f"{len(segs)} segments within {RADIUS_M} m")
    for s in segs: print(f"  id {s['id']} {s['d']:.0f} m {s['color']}")
    render_map(segs)