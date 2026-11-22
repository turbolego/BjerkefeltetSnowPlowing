"""Load and validate the bot configuration from config.toml.

The config path can be overridden with the PLOWBOT_CONFIG environment
variable or the --config command-line option.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.toml")


@dataclass(frozen=True)
class Config:
    name: str
    latitude: float
    longitude: float
    radius_m: float = 150
    min_distance_m: float = 0
    base_url: str = "https://mobiwinqa.bmsystem.se/PlogNorge"
    area: str = "baerum"
    status_group_id: int | None = None
    hashtags: list[str] = field(default_factory=list)
    timezone: str = "Europe/Oslo"
    map_output: str = "plowmap.png"
    map_size_px: int = 600
    map_zoom: int | str = "auto"
    tile_url: str = "https://tile.opentopomap.org/{z}/{x}/{y}.png"
    font: str = ""
    attribution: str = "© OpenStreetMap-bidragsytere, © OpenTopoMap (CC-BY-SA)"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def map_url(self, group_id: int | None = None) -> str:
        gid = group_id if group_id is not None else self.status_group_id
        url = f"{self.base_url}/{self.area}"
        return f"{url}?groupId={gid}" if gid is not None else url


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.getenv("PLOWBOT_CONFIG") or DEFAULT_CONFIG_PATH)
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    loc = raw.get("location", {})
    plog = raw.get("plognorge", {})
    post = raw.get("post", {})
    mp = raw.get("map", {})

    missing = [k for k in ("name", "latitude", "longitude") if k not in loc]
    if missing:
        raise ValueError(f"{path}: [location] is missing {', '.join(missing)}")
    if not plog.get("area"):
        raise ValueError(f"{path}: [plognorge] area is required (e.g. 'baerum')")

    zoom = mp.get("zoom", "auto")
    if zoom != "auto" and not (isinstance(zoom, int) and 1 <= zoom <= 19):
        raise ValueError(f"{path}: [map] zoom must be 1-19 or \"auto\"")

    cfg = Config(
        name=str(loc["name"]),
        latitude=float(loc["latitude"]),
        longitude=float(loc["longitude"]),
        radius_m=float(loc.get("radius_m", 150)),
        min_distance_m=float(loc.get("min_distance_m", 0)),
        base_url=str(plog.get("base_url", Config.base_url)).rstrip("/"),
        area=str(plog["area"]).strip().lower(),
        status_group_id=(
            int(plog["status_group_id"]) if "status_group_id" in plog else None
        ),
        hashtags=[str(h).lstrip("#") for h in post.get("hashtags", [])],
        timezone=str(post.get("timezone", "Europe/Oslo")),
        map_output=str(mp.get("output", "plowmap.png")),
        map_size_px=int(mp.get("size_px", 600)),
        map_zoom=zoom,
        tile_url=str(mp.get("tile_url", Config.tile_url)),
        font=str(mp.get("font", "")),
        attribution=str(mp.get("attribution", Config.attribution)),
    )
    cfg.tz  # fail fast on an invalid time zone name
    return cfg
