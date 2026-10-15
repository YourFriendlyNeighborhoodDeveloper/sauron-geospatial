"""Processing throughput and per-message latency on this machine.

    python -m tools.benchmark [--copies 200] [--json eval/results/benchmark.json]

Builds a workload by copying the bundled fixture log with unique source ids,
processes it into a fresh on-disk SQLite store, then processes it again to
measure replay (all duplicates). Network and Kafka are not involved: this
measures normalization, geocoding, correlation and storage only.
"""
import argparse
import json
import os
import platform
import statistics
import tempfile
import time
from datetime import datetime, timezone

from src.config import load_config
from src.processor import Processor
from src.reports import Reports
from src.store import Store
from tools.demo import DEMO_NOW, demo_geocoder, fixture_log, zone_geometries


def workload(copies):
    base = [json.loads(m) for m in fixture_log()]
    out = []
    for n in range(copies):
        for env in base:
            env = json.loads(json.dumps(env))
            item = env["item"]
            if env["kind"] == "nws":
                item["properties"]["id"] += f".b{n}"
                item["properties"]["references"] = []
            elif env["kind"] == "usgs":
                item["id"] += f"b{n}"
            elif env["kind"] == "open_data":
                item["row_id"] += f"{n:05d}"
            elif env["kind"] == "rss":
                item["guid"] += f"-b{n}"
            elif env["kind"] == "open_meteo":
                item["point"]["name"] += f" {n}"
            out.append(json.dumps(env).encode())
    return out


def run(processor, messages):
    timings = []
    started = time.perf_counter()
    for m in messages:
        t = time.perf_counter()
        processor.handle(m)
        timings.append(time.perf_counter() - t)
    total = time.perf_counter() - started
    timings.sort()
    return {
        "messages": len(messages),
        "seconds": round(total, 3),
        "messages_per_second": round(len(messages) / total, 1),
        "latency_ms_p50": round(statistics.median(timings) * 1000, 3),
        "latency_ms_p95": round(timings[int(len(timings) * 0.95) - 1] * 1000, 3),
        "latency_ms_max": round(timings[-1] * 1000, 3),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--copies", type=int, default=200)
    parser.add_argument("--json")
    args = parser.parse_args()
    cfg = load_config()
    with tempfile.TemporaryDirectory() as tmp:
        cfg["storage"]["path"] = os.path.join(tmp, "bench.db")
        store = Store(cfg["storage"]["path"])
        for zone_id, geometry in zone_geometries().items():
            store.cache_put("nws_zone", zone_id, geometry)
        processor = Processor(store, cfg, demo_geocoder(cfg), Reports(store, os.path.join(tmp, "reports")),
                              clock=lambda: DEMO_NOW)
        messages = workload(args.copies)
        first = run(processor, messages)
        counts = store.counts()
        replay = run(processor, messages)
        after = store.counts()
    result = {
        "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "machine": {"platform": platform.platform(), "python": platform.python_version(),
                    "processor": platform.processor() or platform.machine(), "cpus": os.cpu_count()},
        "workload": f"{args.copies} copies of the {len(fixture_log())}-message fixture log, unique ids",
        "first_pass": first,
        "replay_pass": replay,
        "store_after_first_pass": {"events": counts["events"], "event_versions": counts["event_versions"]},
        "replay_changed_store": counts != after,
    }
    print(json.dumps(result, indent=2))
    if args.json:
        os.makedirs(os.path.dirname(args.json), exist_ok=True)
        with open(args.json, "w") as f:
            json.dump(result, f, indent=2)


if __name__ == "__main__":
    main()
