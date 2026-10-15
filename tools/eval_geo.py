"""Score news-headline geocoding and classification against labeled headlines in eval/.

Compares the coordinates the original pipeline stored for each headline
(the "baseline" field) with the current resolver. Requires the gazetteer:
    python -m tools.fetch_gazetteer
    python -m tools.eval_geo [--json eval/results/geo_eval.json]
"""
import argparse
import json
import os
import sys
from collections import Counter

from src.config import load_config
from src.geo import haversine_km
from src.geocoding import Gazetteer, Geocoder, load_outlets
from src.hazard_enrichment import classify_news

TOLERANCE_KM = {"place": 25, "town": 25, "county": 60}
KINDS = {"place": ("place",), "town": ("place", "cousub"), "county": ("county",)}


def truth_point(gaz, truth):
    matches = [m for m in gaz.lookup(truth["name"], KINDS[truth["kind"]]) if m["state"] == truth["state"]]
    if "near" in truth:
        matches = [m for m in matches if haversine_km(m["lat"], m["lon"], *truth["near"]) <= 50]
    if len(matches) != 1:
        raise SystemExit(f"label does not resolve to exactly one gazetteer entry: {truth} -> {len(matches)}")
    return matches[0]["lat"], matches[0]["lon"]


def score(gaz, row, lat, lon):
    truth = row["truth"]
    placed = lat is not None and lon is not None
    if truth["scope"] == "place":
        if not placed:
            return "unresolved"
        tlat, tlon = truth_point(gaz, truth)
        return "correct" if haversine_km(lat, lon, tlat, tlon) <= TOLERANCE_KM[truth["kind"]] else "wrong"
    if truth["scope"] == "state":
        if not placed:
            return "unresolved"
        return "placed_same_state" if gaz.nearest_state(lat, lon) == truth["state"] else "wrong"
    if truth["scope"] == "non_us":
        return "wrong" if placed else "correct_abstain"
    return "placed_unverifiable" if placed else "abstained"


def summarize(outcomes, rows):
    by_scope = {}
    for row, outcome in zip(rows, outcomes):
        by_scope.setdefault(row["truth"]["scope"], Counter())[outcome] += 1
    total = Counter(outcomes)
    placed = sum(v for k, v in total.items() if k in ("correct", "wrong", "placed_same_state", "placed_unverifiable"))
    located = by_scope.get("place", Counter())
    n_located = sum(located.values())
    return {
        "items": len(rows),
        "placed": placed,
        "wrong": total["wrong"],
        "wrong_share_of_placed": round(total["wrong"] / placed, 3) if placed else None,
        "located_items": n_located,
        "located_correct": located["correct"],
        "located_correct_rate": round(located["correct"] / n_located, 3) if n_located else None,
        "located_wrong": located["wrong"],
        "located_unresolved": located["unresolved"],
        "by_scope": {k: dict(v) for k, v in sorted(by_scope.items())},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", default="eval/geo_labels_dev.jsonl")
    parser.add_argument("--json")
    parser.add_argument("--no-outlets", action="store_true", help="disable the outlet-market constraint")
    args = parser.parse_args()

    cfg = load_config()
    geo_cfg = cfg.get("geocoding") or {}
    gaz = Gazetteer(geo_cfg.get("gazetteer_path", "data/gazetteer.db"))
    if not gaz.available:
        sys.exit("gazetteer missing: run python -m tools.fetch_gazetteer")
    outlets = None if args.no_outlets else load_outlets(geo_cfg.get("outlets_path", "config/outlets.yaml"))
    geocoder = Geocoder(gaz, outlets)

    with open(args.labels) as f:
        rows = [json.loads(line) for line in f if line.strip()]

    baseline, current, details = [], [], []
    for row in rows:
        b = row["baseline"]
        baseline.append(score(gaz, row, b["lat"], b["lon"]))
        r = geocoder.resolve(row["title"], row["outlet"])
        current.append(score(gaz, row, r["lat"], r["lon"]))
        details.append({"id": row["id"], "baseline": baseline[-1], "current": current[-1],
                        "confidence": r["confidence"], "place": r["place"], "region": r["region"]})

    tp = fp = fn = tn = 0
    for row in rows:
        predicted = classify_news(row["title"]) == "incident"
        actual = row["class"] == "incident"
        tp += predicted and actual
        fp += predicted and not actual
        fn += actual and not predicted
        tn += not predicted and not actual

    report = {
        "labels": args.labels,
        "outlet_constraint": not args.no_outlets,
        "baseline": summarize(baseline, rows),
        "current": summarize(current, rows),
        "classification": {
            "incident_precision": round(tp / (tp + fp), 3) if tp + fp else None,
            "incident_recall": round(tp / (tp + fn), 3) if tp + fn else None,
            "true_positive": tp, "false_positive": fp, "false_negative": fn, "true_negative": tn,
            "baseline_note": "the original pipeline kept every headline containing a crash keyword",
            "baseline_incident_precision": round((tp + fn) / len(rows), 3),
        },
        "current_confidence": dict(Counter(d["confidence"] for d in details)),
    }
    print(json.dumps(report, indent=2))
    if args.json:
        os.makedirs(os.path.dirname(args.json), exist_ok=True)
        with open(args.json, "w") as f:
            json.dump({**report, "details": details}, f, indent=2)


if __name__ == "__main__":
    main()
