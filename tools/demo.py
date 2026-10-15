"""Offline demo: replay the bundled source fixtures through the real processor.

    python -m src --demo        # builds data/demo.db and serves http://127.0.0.1:8050

The fixtures were captured from the live sources on 2026-10-15; the demo pins
its clock to that afternoon so the warnings are shown as they were.
"""
import json
import os
import shutil
from datetime import datetime, timezone

import feedparser

from src.geocoding import Gazetteer, Geocoder, build_gazetteer, load_outlets
from src.processor import Processor
from src.reports import Reports
from src.store import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
DEMO_NOW = datetime(2026, 10, 15, 17, 0, tzinfo=timezone.utc)
FETCHED_AT = "2026-10-15T16:59:00+00:00"


def _load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f) if name.endswith(".json") else f.read()


def _env(kind, feed, item):
    return {"v": 2, "feed": feed, "kind": kind, "fetched_at": FETCHED_AT, "item": item}


def zone_geometries():
    folder = os.path.join(FIXTURES, "nws_zones")
    return {name[:-5]: _load(f"nws_zones/{name}")["geometry"] for name in sorted(os.listdir(folder))}


def fixture_log():
    """Raw envelopes in the order the feeds would publish them."""
    weather = _load("open_meteo_calm.json")
    rss = []
    for e in feedparser.parse(_load("news_rss.xml").encode()).entries:
        rss.append({"guid": e.id, "title": e.title, "link": e.link,
                    "published": "2026-10-15T15:00:00+00:00" if "Magna" in e.title else "2026-10-15T12:00:00+00:00",
                    "source_name": e.source.title, "source_url": e.source.href})
    gusty = {"point": {"name": "Denver", "lat": 39.7392, "lon": -104.9903},
             "current": {**weather["current"], "wind_gusts_10m": 95.0, "time": "2026-10-15T16:45"},
             "units": weather["current_units"]}
    messages = [_env("nws", "nws_alerts", f) for f in _load("nws_active.json")["features"]]
    messages += [_env("usgs", "usgs_quakes", f) for f in _load("usgs_feed.json")["features"]]
    messages += [_env("open_data", "sf_traffic_collisions", r) for r in _load("datasf_traffic_collisions.json")]
    messages += [_env("rss", "news_rss", r) for r in rss]
    messages.append(_env("open_meteo", "open_meteo", gusty))
    return [json.dumps(m).encode() for m in messages]


def demo_geocoder(cfg):
    geo_cfg = cfg.get("geocoding") or {}
    path = geo_cfg.get("gazetteer_path", "data/gazetteer.db")
    if not os.path.exists(path):
        path = os.path.join(os.path.dirname(cfg["storage"]["path"]) or ".", "demo-gazetteer.db")
        build_gazetteer({k: os.path.join(FIXTURES, "gazetteer", f"{k}.txt") for k in ("place", "cousub", "county")}, path)
    return Geocoder(Gazetteer(path), load_outlets(geo_cfg.get("outlets_path", "config/outlets.yaml")))


def load_demo(cfg):
    db = cfg["storage"]["path"]
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db + suffix):
            os.remove(db + suffix)
    reports_dir = (cfg.get("reports") or {}).get("dir", "data/demo-reports")
    shutil.rmtree(reports_dir, ignore_errors=True)
    store = Store(db)
    for zone_id, geometry in zone_geometries().items():
        store.cache_put("nws_zone", zone_id, geometry)
    reports = Reports(store, reports_dir)
    processor = Processor(store, cfg, demo_geocoder(cfg), reports, clock=lambda: DEMO_NOW)
    for message in fixture_log():
        processor.handle(message)
    reports.export_csv()
    for feed, kind in (("nws_alerts", "nws"), ("usgs_quakes", "usgs"), ("sf_traffic_collisions", "open_data"),
                       ("news_rss", "rss"), ("open_meteo", "open_meteo")):
        store.record_poll(feed, kind, 60, FETCHED_AT, True, items=None, published=None)
    cfg.setdefault("dashboard", {})["reference_date"] = DEMO_NOW.date().isoformat()
    return store
