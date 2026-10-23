"""Export the offline demo as static data for the project site (site/).

    python -m tools.build_site [--out site/data/demo.json]

Replays the bundled source fixtures through the real processor, exactly as
`python -m src --demo` does, then writes what the dashboard and API would
serve: map layers, incident rows, per-incident evidence and history, feed
health and sample API responses. The fixture gazetteer is always used so the
output is the same on every machine. No network or Kafka.
"""
import argparse
import copy
import json
import os
import tempfile

from flask import Flask

from src.api import feed_health, install_api
from src.config import load_config
from src.dashboard import location_basis, map_payload, table_rows
from src.schema import CLEAR_REASONS
from tools.demo import DEMO_NOW, ROOT, load_demo

DESCRIPTION_CHARS = 700
API_EXAMPLES = (
    "/api/v1/incidents?state=all&limit=2",
    "/api/v1/alerts/at?lat=33.636&lon=-99.864&start=2026-10-15T00:00:00Z&end=2026-10-16T00:00:00Z",
)


def demo_store(workdir):
    cfg = copy.deepcopy(load_config(os.path.join(ROOT, "config", "config.yaml")))
    cfg["storage"]["path"] = os.path.join(workdir, "demo.db")
    cfg["reports"]["dir"] = os.path.join(workdir, "reports")
    cfg["geocoding"]["gazetteer_path"] = os.path.join(workdir, "missing-gazetteer.db")
    cfg["geocoding"]["outlets_path"] = os.path.join(ROOT, "config", "outlets.yaml")
    return load_demo(cfg)


def incident_detail(store, incident_id):
    detail = store.incident_detail(incident_id)
    inc = detail["incident"]
    description = inc.get("description") or ""
    if len(description) > DESCRIPTION_CHARS:
        description = description[:DESCRIPTION_CHARS].rstrip() + " …"
    return {
        "title": inc["title"], "state": inc["state"], "category": inc["category"], "type": inc["type"],
        "subtype": inc.get("subtype"), "severity": inc["severity"], "verification": inc["verification"],
        "location_basis": location_basis(inc), "location_confidence": inc["location_confidence"],
        "place": inc.get("place"), "region": inc.get("region"), "road": inc.get("road"),
        "event_time": inc["event_time"], "expires_at": inc.get("expires_at"), "cleared_at": inc.get("cleared_at"),
        "clear_reason": CLEAR_REASONS.get(inc.get("clear_reason")), "description": description,
        "evidence": [{
            "source": e["event"]["source"], "reliability": e["event"]["source_reliability"],
            "title": e["event"]["title"], "url": e["event"].get("source_url"), "version": e["version"],
            "published_at": e["event"].get("published_at"), "severity_basis": e["event"].get("severity_basis"),
            "location": {k: e["event"]["location"].get(k) for k in ("confidence", "precision", "evidence")},
        } for e in detail["evidence"]],
        "history": [{"at": h["at"], "from": h["from_state"], "to": h["to_state"], "reason": h["reason"]}
                    for h in detail["history"]],
    }


def api_examples(store):
    server = Flask(__name__)
    install_api(server, store)
    client = server.test_client()
    return {path: client.get(path).get_json() for path in API_EXAMPLES}


def build(workdir):
    store = demo_store(workdir)
    rows = store.query_incidents(state="all", limit=3000, geometry=True)
    incidents, _ = table_rows(rows)
    for row, inc in zip(rows, incidents):
        inc["state"] = row["state"]
        inc["mapped"] = row["location_confidence"] not in ("geocoded_low", "unresolved")
    counts = store.counts()
    return {
        "reference_time": DEMO_NOW.isoformat(),
        "source": "tests/fixtures: responses captured from the live sources on 2026-10-15",
        "counts": {"incidents": counts["incidents"], "events": counts["events"],
                   "event_versions": counts["event_versions"], "dead_letters": counts["dead_letters"]},
        "feeds": [{k: f[k] for k in ("feed", "kind", "status")} for f in feed_health(store, now=DEMO_NOW)],
        "map": map_payload(rows, "demo"),
        "incidents": incidents,
        "details": {r["incident_id"]: incident_detail(store, r["incident_id"]) for r in rows},
        "api": api_examples(store),
    }


def main():
    parser = argparse.ArgumentParser(prog="python -m tools.build_site")
    parser.add_argument("--out", default=os.path.join(ROOT, "site", "data", "demo.json"))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as workdir:
        data = build(workdir)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(data, f, separators=(",", ":"), sort_keys=True)
    print(f"wrote {args.out}: {len(data['incidents'])} incidents, {data['map']['layers']}, "
          f"{os.path.getsize(args.out) // 1024} KB")


if __name__ == "__main__":
    main()
