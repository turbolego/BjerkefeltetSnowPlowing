#!/usr/bin/env python3
"""
Bjerkefeltet Snow Plow Bot
Monitors PlogNorge for snow plow activity near Bjerkehaugen 6
and posts updates to Bluesky.
"""

import os
import re
import json
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path
from atproto import Client
from dotenv import load_dotenv

load_dotenv()

# Configuration
TARGET = {"lat":59.9636663, "lng":10.4678833}  # Bjerkehaugen 6
TARGET_RADIUS_M = 200  # Only report segments within 200m
PLOGNORE_URL = "https://mobiwinqa.bmsystem.se/PlogNorge"
BSKY_HANDLE = os.getenv("BSKY_HANDLE")
BSKY_PASSWORD = os.getenv("BSKY_PASSWORD")

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Global session
s = requests.Session()
s.headers.update({"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"})


def get_plognore_data():
    """Fetch PlogNorge data and return segments near our target"""
    try:
        # 1) Get page to establish session + grab anti-forgery token
        r = s.get(f"{PLOGNORE_URL}/baerum?groupId=80", timeout=30)
        m = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r.text)
        if not m:
            raise ValueError("Could not find anti-forgery token")
        token = m.group(1)

        # 2) POST GetVisits with that token + same session
        pr = s.post(f"{PLOGNORE_URL}/Home/GetVisits",
                    json={"Date":datetime.now(timezone.utc).isoformat(), "StatusGroupId":80},
                    headers={"X-Requested-With":"XMLHttpRequest",
                             "RequestVerificationToken":token,
                             "Content-Type":"application/json; charset=utf-8"},
                    timeout=60)
        pr.raise_for_status()
        d = pr.json()

        # 3) Filter segments near our target
        mv = d.get("MaintenanceObjectVisits") or []
        def wkt_to_pts(w):
            m = re.findall(r"(-?\d+\.\d+)\s+(-?\d+\.\d+)", w)  # Wkt = "lng lat"
            return [(float(lng), float(lat)) for lng, lat in m]

        def haversine(a_lat,a_lng,b_lat,b_lng):
            R=6371000
            p1=math.radians(a_lat); p2=math.radians(b_lat)
            dp=math.radians(b_lat-a_lat); dl=math.radians(b_lng-a_lng)
            h=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
            return 2*R*math.asin(math.sqrt(h))

        def min_dist_to_seg(pts):
            best=float('inf')
            for (lng,lat) in pts:
                best=min(best, haversine(TARGET["lat"],TARGET["lng"],lat,lng))
            return best

        rows=[]
        for v in mv:
            w=v.get("Wkt") or ""
            if "LINESTRING" not in w and "POINT" not in w: continue
            pts=wkt_to_pts(w)
            if not pts: continue
            dm=min_dist_to_seg(pts)
            if dm<TARGET_RADIUS_M:
                rows.append({
                    "distance_m":dm,
                    "id":v.get("Id"),
                    "color":v.get("Color"),
                    "last_visit":v.get("LatestVisitTime"),
                    "points":len(pts),
                    "wkt":w[:60]
                })

        rows.sort(key=lambda x:x["distance_m"])
        return rows
    except Exception as e:
        logging.error(f"Error fetching PlogNorge data: {e}")
        return []


def post_to_bluesky(text):
    """Post text to Bluesky"""
    if not BSKY_HANDLE or not BSKY_PASSWORD:
        logging.error("Bluesky credentials not configured")
        return False

    try:
        client = Client()
        client.login(BSKY_HANDLE.strip(), BSKY_PASSWORD.strip())
        client.send_post(text)
        logging.info(f"Posted to Bluesky: {text[:50]}...")
        return True
    except Exception as e:
        logging.error(f"Error posting to Bluesky: {e}")
        return False


def main():
    """Main execution"""
    logging.info("Starting Bjerkefeltet Snow Plow Bot")

    # Get data
    segments = get_plognore_data()
    if not segments:
        logging.warning("No segments found near target")
        return

    # Prepare post
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    oldest = min(s["last_visit"] for s in segments if s["last_visit"])
    oldest_dt = datetime.fromisoformat(oldest.replace("Z","+00:00"))
    days_since = (datetime.now(timezone.utc) - oldest_dt).days

    post_text = f"""
Bjerkefeltet Snow Plow Update ({now})

Near Bjerkehaugen 6 (Lommedalen, Bærum):

- {len(segments)} road segments within {TARGET_RADIUS_M}m
- Oldest plow activity: {days_since} days ago
- Last plow: {oldest_dt.strftime('%Y-%m-%d %H:%M')} UTC

#SnowPlowing #Bjerkefeltet #Bærum #Norway
"""

    # Post
    if post_to_bluesky(post_text):
        logging.info("Successfully posted update")
    else:
        logging.error("Failed to post update")

if __name__ == "__main__":
    main()