# Bjerkefeltet Snow Plow Bot 🚜

Automated bot that monitors snow plow activity near **Bjerkehaugen 6**
(Lommedalen, Bærum) using the PlogNorge (Mobiwin) winter-maintenance data for
Bærum, and posts the plow status to [Bluesky](https://bsky.app).

## Target

- **Address:** Bjerkehaugen 6, Lommedalen, Bærum (kommune 3201-100/152)
- **Coordinates:** 59.9636663, 10.4678833
- **Radius:** 150 m around the address
- **Data source:** PlogNorge Bærum status group 80
  ("Vintervedlikehold kommunalt veinett")
  - Live map: https://mobiwinqa.bmsystem.se/PlogNorge/baerum?groupId=80
  - API: `POST /Home/GetVisits` (anti-forgery token obtained from the page)

## How it works

1. GETs the Bærum PlogNorge page to establish a session and capture the
   ASP.NET `__RequestVerificationToken`.
2. POSTs `/Home/GetVisits` with `StatusGroupId: 80` and parses all road /
   maintenance segments (WKT LINESTRING geometry + `LatestVisitTime`).
3. Keeps only the segments whose geometry comes within 150 m of Bjerkehaugen 6.
4. Computes the most recent plow time in the area and composes a post:
   - How many segments are nearby
   - Most recent / oldest plow time
   - How many segments have no recorded plow visit at all
   - A link to the live PlogNorge map
5. Renders an annotated map image (`render_map.py`) and attaches it to the
   post:
   - Base map: OSM tiles (fetched with 0.4 s spacing to stay within OSM's
     tile usage policy). Blocked "418" placeholder tiles are detected by
     their colour-bucket signature and rejected.
   - If raster tiles are unavailable (e.g. the CI IP is blocked by OSM),
     it falls back to a clean vector schematic so a post image always
     exists.
   - Overlay: the segments in PlogNorge status colours, a white 150 m ring
     around the address, the address marker, a Norwegian title/subtitle,
     a legend and attribution.
   - `assets/sample_map_real_tiles.png` shows the image with a real street
     base (PlogNorge's own tile server) as a reference.

Status thresholds (from PlogNorge `GetStatusGroups`):
| Color | Meaning |
|-------|---------|
| `#33ff00` green | Plowed within the last 4 h |
| `#3333ff` blue | Plowed within the last 12 h |
| `#ff0000` red | Not plowed recently |

## Example post

```
🚜 Plog-status for Bjerkehaugen 6, Lommedalen (Bærum) (18.11.2026 21.47 CET):

• 7 road segments within 150 m
• Most recent plow: 13.11.2026 kl 22.12 (4,9 dager siden)
• Oldest plow in area: 13.11.2026 kl 21.47

Kart: https://mobiwinqa.bmsystem.se/PlogNorge/baerum?groupId=80

#Brøyteweb #bærum #bjerkefeltet #snø #vintervedlikehold
```

## Setup

1. Create a dedicated Bluesky account for the bot (or use your own).
2. Configure credentials. Locally, copy `.env.example` → `.env` and fill in:
   ```
   BSKY_HANDLE=your_handle.bsky.social
   BSKY_PASSWORD=your_app_password
   ```
   On GitHub Actions, set the same two as repo secrets.
3. Install dependencies (Python 3.11 recommended; `atproto` requires <3.12):
   ```
   pip install -r requirements.txt
   ```

## Usage

```
# Print the would-be post without sending it (no credentials needed)
python plowbot.py --dry-run

# Fetch data + print only (also skips posting)
python plowbot.py --no-post

# Fetch + post to Bluesky (requires BSKY_HANDLE / BSKY_PASSWORD)
python plowbot.py
```

## Deployment

GitHub Actions runs the bot on a schedule (`.github/workflows/plowbot.yml`).
Snow-plow monitoring is most useful in winter, so the cron is tuned to run
several times a day in November–March.

## Reference

- PlogNorge live map: https://mobiwinqa.bmsystem.se/PlogNorge/baerum?groupId=80
- Kartverket property record for Bjerkehaugen 6:
  https://eiendomsregisteret.kartverket.no/eiendom/3201/100/152/0/0
- Bot pattern borrowed from GULBILBOT/gul-bil-bluesky
