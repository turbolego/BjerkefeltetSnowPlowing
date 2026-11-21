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

def mercator_to_pixel(mx, my, img_w, img_h):
    """Convert mercator meters to pixel position in the full image (global mapping)."""
    # Global mapping: world extent to image dimensions
    world = 2 * math.pi * 6378137.0
    px_x = int((mx + world/2) / world * img_w)
    py_y = int((world/2 - my) / world * img_h)  # invert Y
    return px_x, py_y

def haversine_m(lat1, lng1, lat2, lng2):
    R=6371000.0; p1,p2=math.radians(lat1),math.radians(lat2)
    dp,dl=math.radians(lat2-lat1),math.radians(lng2-lng1)
    h=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(h))

def parse_wkt(wkt):
    return [(float(a),float(b)) for a,b in re.findall(r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?", wkt)]

def fetch_segments():
    s=requests.Session(); s.headers.update({"User-Agent":"Mozilla/5.0"})
    r=s.get("https://mobiwinqa.bmsystem.se/PlogNorge/baerum?groupId=80",timeout=30); r.raise_for_status()
    tok=re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"',r.text).group(1)
    pr=s.post("https://mobiwinqa.bmsystem.se/PlogNorge/Home/GetVisits",
              json={"Date":datetime.now(timezone.utc).isoformat(),"StatusGroupId":80},
              headers={"X-Requested-With":"XMLHttpRequest","RequestVerificationToken":tok,
                       "Content-Type":"application/json; charset=utf-8"},timeout=120)
    pr.raise_for_status(); return pr.json().get("MaintenanceObjectVisits") or []

def pick_nearby(all_segments):
    out=[]
    for seg in all_segments:
        w=seg.get("Wkt") or ""
        if not re.search(r"LINESTRING|POINT", w): continue
        pts=parse_wkt(w)
        if not pts: continue
        dm=min(haversine_m(TARGET["lat"],TARGET["lng"],la,ln) for ln,la in pts)
        if dm<=RADIUS_M: out.append({"id":seg.get("Id"),"color":seg.get("Color"),"last_visit":seg.get("LatestVisitTime"),"d":dm,"pts":pts})
    out.sort(key=lambda x:x["d"]); return out

def _is_real_tile(content: bytes) -> bool:
    """A real OSM tile has high pixel variance (>800); blocked placeholders are uniform."""
    if len(content)<500 or content[:4]!=b"\x89PNG": return False
    try:
        im=Image.open(io.BytesIO(content)).convert("RGB")
        if im.size[0]<256: return False
        px=im.load(); v=[]
        for y in range(0,im.size[1],4):
            for x in range(0,im.size[0],4): r,g,b=px[x,y]; v.extend([r,g,b])
        return st.pvariance(v)>800 if v else False
    except Exception:
        return False

def fetch_base_map():
    """Download OSM-compatible tiles aligned to TARGET position."""
    # Tile coords for Bjerkehaugen 6 at z17
    t_merc_x, t_merc_y = lonlat_to_mercator_m(TARGET["lng"], TARGET["lat"])
    n = 2 ** ZOOM
    world = 2 * math.pi * 6378137.0
    tile_size = world / n
    x_t = int((t_merc_x + world/2) / tile_size)
    y_t = int((world/2 - t_merc_y) / tile_size)
    print(f"  tile center [{x_t},{y_t}] for Bjerkehaugen 6")
    
    sources=[
        f"https://tile.openstreetmap.org/{ZOOM}"   # Standard OSM tiles
    ]
    tiles=[]
    for src_url in sources:
        if len(tiles)>=9: break
        for dy in (-1,0,1):
            for dx in (-1,0,1):
                if len(tiles)>=9: break
                url=f"{src_url}/{x_t+dx}/{y_t+dy}.png"
                r=requests.get(url,headers={"User-Agent":"Mozilla/5.0"},timeout=15)
                if r.status_code==200 and _is_real_tile(r.content):
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
    cx, cy = mercator_to_pixel(t_merc_x, t_merc_y, IMG_W, IMG_H)
    print(f"  target maps to pixel ({cx},{cy}) in {IMG_W}x{IMG_H} image")
    
    # Draw segments using consistent coordinate system
    for seg in sorted(segments,key=lambda s:-s["d"]):
        key=STATUS_COLORS.get(seg.get("color"),STATUS_COLORS["#ff0000"]); color=key[1]
        pts=[]
        for lng,la in seg["pts"]:
            mx,my = lonlat_to_mercator_m(lng,la)
            sx,sy = mercator_to_pixel(mx, my, IMG_W, IMG_H)
            pts.append((sx,sy))
        if len(pts)>1:
            d.line(pts, fill=color, width=3, joint="curve")
    
    # Small marker dot at target (address location)
    d.ellipse([cx-4,cy-4,cx+4,cy+4],fill=(20,20,20))
    
    # Title/subtitle
    title="Plog-status: "+TARGET_NAME
    sub=f"{len(segments)} seg. < {RADIUS_M} m · sist 13.11"
    ft=ImageFont.load_default(); fs=ImageFont.load_default()
    tb=d.textbbox((0,0),title,font=ft); tw,th=tb[2]-tb[0],tb[3]-tb[1]
    sb=d.textbbox((0,0),sub,font=fs); sw,sh=sb[2]-sb[0],sb[3]-sb[1]
    pad=12
    d.rectangle([pad,pad,pad+max(tw,sw)+pad*2,pad+th+sh+pad*2],fill=(255,255,255),outline=(20,20,20),width=2)
    d.text((pad+2,pad+2),title,font=ft,fill=(10,10,10)); d.text((pad+2,pad+th+6),sub,font=fs,fill=(40,40,40))
    
    # Legend
    leg=[((226,28,28),"Ikke pløyd nylig (>12t)"),
         ((48,110,230),"Pløyd siste 12t"),
         ((40,190,70),"Pløyd siste 4t")]
    lx,ly=pad,IMG_H-pad-70
    for i,(clr,lb) in enumerate(leg):
        yy=ly+i*22; d.line([(lx,yy+10),lx+16,yy+10],fill=clr,width=5)
        d.text((lx+24,yy),lb,font=ft,fill=(20,20,20))
    
    # Attribution
    a="© OSM-contributorer | PlogNorge"; ab=d.textbbox((0,0),a,font=ft); aw=ab[2]-ab[0]
    d.rectangle([IMG_W-aw-12,IMG_H-32,IMG_W-4,IMG_H-12],fill=(255,255,255),outline=(20,20,20),width=1)
    d.text((IMG_W-aw-8,IMG_H-26),a,font=ft,fill=(70,70,70))
    
    base.save(out_path)
    print(f"  saved {out_path} ({IMG_W}x{IMG_H})")
    return out_path

if __name__=="__main__":
    from plowbot import get_plognorge_segments, segments_near_target
    segs=segments_near_target(get_plognorge_segments())
    print(f"{len(segs)} segments within {RADIUS_M} m")
    for s in segs: print(f"  id {s['id']} {s['d']:.0f} m {s['color']}")
    render_map(segs)
