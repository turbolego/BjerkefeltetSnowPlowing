#!/usr/bin/env python3
"""render_map.py - render a snow-plow status map image for Bjerkehaugen 6 (200 m radius, 250x250 px, no ring)."""

import io, math, re, time
from datetime import datetime, timezone
import requests
from PIL import Image, ImageDraw, ImageFont

TARGET = {"lat": 59.9636663, "lng": 10.4678833}
TARGET_NAME = "Bjerkehaugen 6, Lommedalen"
RADIUS_M = 200         # radius around address for segment filtering
ZOOM = 17
IMG_W = IMG_H = 250    # fixed 250x250 output

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

def _tile_color_buckets(content: bytes) -> int:
    try:
        im=Image.open(io.BytesIO(content)).convert("RGB")
        if im.size[0]<256: return 0
        px=im.load(); seen=set()
        for y in range(0,im.size[1],2):
            for x in range(0,im.size[0],2):
                r,g,b=px[x,y]; seen.add((r//32,g//32,b//32))
        return len(seen)
    except: return 0

def _is_real_tile(content: bytes) -> bool:
    return len(content)>500 and content[:4]==b"\x89PNG" and _tile_color_buckets(content)>=40

def fetch_base_map():
    x_t,y_t=69360,38086
    tiles=[]
    for dy in (-1,0,1):
        for dx in (-1,0,1):
            url=f"https://tile.openstreetmap.org/{ZOOM}/{x_t+dx}/{y_t+dy}.png"
            r=requests.get(url,headers={"User-Agent":"Mozilla/5.0"},timeout=15)
            if r.status_code==200 and _is_real_tile(r.content): tiles.append(Image.open(io.BytesIO(r.content)).convert("RGB"))
            else: print(f"  tile {x_t+dx},{y_t+dy} blocked/not-real status={r.status_code} len={len(r.content)}")
            time.sleep(0.2)
    if not tiles: return None
    base=Image.new("RGB",(IMG_W,IMG_H),(232,234,236))
    for i,tile in enumerate(tiles):
        sx,sy = i%3*83+1, i//3*83+1; base.paste(tile.resize((83,83),Image.LANCZOS),(sx,sy))
    return base

def vector_base():
    img=Image.new("RGB",(IMG_W,IMG_H),(250,250,250)); d=ImageDraw.Draw(img)
    for x in range(0,IMG_W,25): d.line([(x,0),(x,IMG_H)],fill=(200,200,200),width=1)
    for y in range(0,IMG_H,25): d.line([(0,y),(IMG_W,y)],fill=(200,200,200),width=1); return img

def render_map(segments, out_path="plowmap.png"):
    base=fetch_base_map()
    if base is None: print("  OSM tiles unavailable - using vector fallback"); base=vector_base()
    else: base=base.resize((IMG_W,IMG_H),Image.LANCZOS)
    d=ImageDraw.Draw(base); cx,cy=IMG_W//2,IMG_H//2
    # px per metre: full image spans 2*RADIUS_M, so 250px / 400m
    px_per_m = IMG_W / (2.0*RADIUS_M)
    t_merc_x, t_merc_y = lonlat_to_mercator_m(TARGET["lng"], TARGET["lat"])
    # draw segments
    for seg in sorted(segments,key=lambda s:-s["d"]):
        key=STATUS_COLORS.get(seg.get("color"),STATUS_COLORS["#ff0000"]); color=key[1]
        pts=[]
        for lng,la in seg["pts"]:
            mx,my=lonlat_to_mercator_m(lng,la)
            sx=cx + (mx - t_merc_x) * px_per_m
            sy=cy - (my - t_merc_y) * px_per_m
            pts.append((sx,sy))
        if len(pts)>1:
            d.line(pts, fill=color, width=2, joint="curve")
    # NO white ring - per user request
    d.ellipse([cx-3,cy-3,cx+3,cy+3],fill=(20,20,20))
    # title/subtitle
    title="Plog-status: "+TARGET_NAME
    sub=f"{len(segments)} veistrekker < {RADIUS_M} m · sist plogget 13.11"
    ft=ImageFont.load_default(); fs=ImageFont.load_default()
    tb=d.textbbox((0,0),title,font=ft); tw,th=tb[2]-tb[0],tb[3]-tb[1]
    sb=d.textbbox((0,0),sub,font=fs); sw,sh=sb[2]-sb[0],sb[3]-sb[1]
    pad=8
    d.rectangle([pad,pad,pad+max(tw,sw)+pad*2,pad+th+sh+pad*2],fill=(255,255,255),outline=(20,20,20),width=2)
    d.text((pad+2,pad+2),title,font=ft,fill=(10,10,10)); d.text((pad+2,pad+th+6),sub,font=fs,fill=(40,40,40))
    # bottom-left legend
    leg=[((226,28,28),"Ikke plogget nylig (>12t)"),((48,110,230),"Plogget siste 12t"),((40,190,70),"Plogget siste 4t")]
    lx,ly=pad,IMG_H-pad-50
    for i,(clr,lb) in enumerate(leg):
        yy=ly+i*18; d.line([(lx,yy+9),lx+15,yy+9],fill=clr,width=5); d.text((lx+22,yy),lb,font=ft,fill=(20,20,20))
    # bottom-right attribution
    a="© OSM-contributorer · PlogNorge"; ab=d.textbbox((0,0),a,font=ft); aw=ab[2]-ab[0]
    d.rectangle([IMG_W-aw-8,IMG_H-24,IMG_W-4,IMG_H-4],fill=(255,255,255),outline=(20,20,20),width=1); d.text([IMG_W-aw-4,IMG_H-20],a,font=ft,fill=(70,70,70))
    base.save(out_path); print("  saved",out_path,base.size); return out_path

if __name__=="__main__":
    from plowbot import get_plognorge_segments, segments_near_target
    segs=segments_near_target(get_plognorge_segments()); print(f"{len(segs)} segments within {RADIUS_M} m")
    for s in segs: print(f"  id {s['id']} {s['d']:.0f} m {s['color']}")
    render_map(segs)
