"""Download the US Census Gazetteer files (public domain) and build data/gazetteer.db.

    python -m tools.fetch_gazetteer
"""
import os
import sys

import requests

from src.config import load_config, user_agent
from src.geocoding import build_gazetteer

BASE = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/{year}_Gazetteer/{year}_Gaz_{name}_national.zip"
FILES = {"place": "place", "cousub": "cousubs", "county": "counties"}


def main():
    cfg = load_config()
    geo_cfg = cfg.get("geocoding") or {}
    year = geo_cfg.get("gazetteer_year", 2025)
    folder = geo_cfg.get("gazetteer_dir", "data/gazetteer")
    os.makedirs(folder, exist_ok=True)
    paths = {}
    for kind, name in FILES.items():
        url = BASE.format(year=year, name=name)
        path = os.path.join(folder, os.path.basename(url))
        if not os.path.exists(path):
            print(f"downloading {url}")
            resp = requests.get(url, headers={"User-Agent": user_agent(cfg)}, timeout=60)
            if resp.status_code != 200:
                sys.exit(f"download failed ({resp.status_code}): {url}")
            with open(path, "wb") as f:
                f.write(resp.content)
        paths[kind] = path
    rows = build_gazetteer(paths, geo_cfg.get("gazetteer_path", "data/gazetteer.db"))
    print(f"gazetteer ready: {rows} names")


if __name__ == "__main__":
    main()
