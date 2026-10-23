import json
import os

from tests.conftest import ROOT
from tools.build_site import build

SITE_DATA = os.path.join(ROOT, "site", "data", "demo.json")


def test_export_draws_exactly_the_reliably_located_incidents(tmp_path):
    data = build(str(tmp_path))
    ids = {r["id"] for r in data["incidents"]}
    assert ids == set(data["details"])
    drawn = {a["id"] for a in data["map"]["areas"]} | {p["id"] for p in data["map"]["points"]}
    assert drawn == {r["id"] for r in data["incidents"] if r["mapped"]}
    assert any(not r["mapped"] for r in data["incidents"])
    assert data["counts"]["dead_letters"] == 0
    assert all(f["status"] == "ok" for f in data["feeds"])


def test_export_carries_real_api_responses(tmp_path):
    api = build(str(tmp_path))["api"]
    alerts = next(v for k, v in api.items() if k.startswith("/api/v1/alerts/at"))
    assert [a["incident_id"] for a in alerts["data"]] == ["inc_38ce0a8ab3c8014c"]
    incidents = next(v for k, v in api.items() if k.startswith("/api/v1/incidents"))
    assert len(incidents["data"]) == 2 and incidents["next_cursor"]


def test_committed_site_data_is_current(tmp_path):
    with open(SITE_DATA) as f:
        committed = json.load(f)
    assert committed == json.loads(json.dumps(build(str(tmp_path)))), "site data is stale: run python -m tools.build_site"
