#!/usr/bin/env python3
"""Discover which Norwegian municipalities have a public PlogNorge map.

Fetches the official municipality list from Kartverket and probes
https://mobiwinqa.bmsystem.se/PlogNorge/<slug> for common ASCII spellings
of each name. Prints the areas found and their status groups.

    python scripts/discover_areas.py
"""

import itertools
import re
import time

import requests

BASE = "https://mobiwinqa.bmsystem.se/PlogNorge"
KOMMUNER = "https://ws.geonorge.no/kommuneinfo/v1/kommuner"


def slug_variants(name: str) -> set[str]:
    name = name.lower()
    out = set()
    for ae, oe, aa, sp in itertools.product(("ae", "a"), ("o", "oe"), ("a", "aa"), ("", "-")):
        s = name.replace("æ", ae).replace("ø", oe).replace("å", aa).replace(" ", sp)
        s = re.sub(r"[^a-z0-9-]", "", s)
        if s:
            out.add(s)
    return out


def main() -> None:
    names = set()
    for k in requests.get(KOMMUNER, timeout=30).json():
        for n in (k["kommunenavn"], k["kommunenavnNorsk"]):
            names.add(n)
            names.update(p.strip() for p in n.split(" - "))
    slugs = sorted(set().union(*(slug_variants(n) for n in names)))
    print(f"Probing {len(slugs)} slugs ...")

    for slug in slugs:
        s = requests.Session()
        r = s.get(f"{BASE}/{slug}", allow_redirects=False, timeout=20)
        if r.status_code == 200:
            groups = s.get(f"{BASE}/Home/GetStatusGroups", timeout=20).json()
            desc = ", ".join(f"{g['Id']} ({g['Name']})" for g in groups)
            print(f"FOUND {slug:<15} {BASE}/{slug}  groups: {desc}", flush=True)
        time.sleep(0.05)


if __name__ == "__main__":
    main()
