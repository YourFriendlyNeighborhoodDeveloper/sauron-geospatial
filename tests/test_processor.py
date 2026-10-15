import copy
import json
import os
import sqlite3
import threading
from collections import namedtuple

import pytest

from src.processor import Processor, consume_kafka
from src.reports import Reports
from src.store import Store
from tests.conftest import Clock, envelope, fixture
from tools.demo import fixture_log

Record = namedtuple("Record", "value timestamp offset")
TP = namedtuple("TP", "topic partition")


def raw_log():
    return fixture_log()


def dump(store):
    events = store.read("SELECT event_id, version, content_hash, incident_id, first_observed FROM source_events "
                        "ORDER BY event_id, version")
    incidents = store.read("SELECT * FROM incidents ORDER BY incident_id")
    history = store.read("SELECT * FROM incident_history ORDER BY incident_id, at, to_state, reason")
    return events, incidents, history


def make_processor(tmp_path, name, cfg, geocoder, clock, zones=True):
    store = Store(str(tmp_path / f"{name}.db"))
    if zones:
        for zone in os.listdir(os.path.join(os.path.dirname(__file__), "fixtures", "nws_zones")):
            store.cache_put("nws_zone", zone[:-5], fixture(f"nws_zones/{zone}")["geometry"])
    return Processor(store, cfg, geocoder, Reports(store, str(tmp_path / f"{name}-reports")), clock=clock), store


def test_processing_the_same_log_twice_changes_nothing(processor, store):
    log = raw_log()
    for m in log:
        processor.handle(m)
    first = dump(store)
    reports = sorted(os.listdir(processor.reports.dir))
    mtimes = {f: os.path.getmtime(os.path.join(processor.reports.dir, f)) for f in reports}
    summaries = [processor.handle(m) for m in log]
    assert dump(store) == first
    assert all(s["stored"] == 0 for s in summaries)
    assert sorted(os.listdir(processor.reports.dir)) == reports
    assert all(os.path.getmtime(os.path.join(processor.reports.dir, f)) == t for f, t in mtimes.items())


def test_full_replay_into_a_fresh_store_is_identical(tmp_path, cfg, geocoder):
    clock = Clock("2026-10-15T17:00:00+00:00")
    a, store_a = make_processor(tmp_path, "a", cfg, geocoder, clock)
    b, store_b = make_processor(tmp_path, "b", cfg, geocoder, clock)
    for m in raw_log():
        a.handle(m)
    for m in raw_log():
        b.handle(m)
    assert dump(store_a) == dump(store_b)
    report_a = sorted(os.listdir(a.reports.dir))
    assert report_a == sorted(os.listdir(b.reports.dir))
    for name in report_a:
        with open(os.path.join(a.reports.dir, name)) as fa, open(os.path.join(b.reports.dir, name)) as fb:
            assert fa.read() == fb.read()


def test_log_produces_expected_incident_mix(processor, store):
    for m in raw_log():
        processor.handle(m)
    rows = store.read("SELECT category, type, state, verification FROM incidents ORDER BY category, type")
    cats = {(r["category"], r["type"]) for r in rows}
    assert ("seismic", "earthquake") in cats and ("weather", "flooding") in cats and ("weather", "wind") in cats
    assert ("roadway", "accident") in cats
    assert all(r["category"] != "roadway" or r["type"] in ("accident", "closure") for r in rows)
    assert store.read("SELECT COUNT(*) n FROM incidents WHERE lat = 0 AND lon = 0")[0]["n"] == 0


@pytest.mark.parametrize("message,reason", [
    (b"not json at all", "malformed envelope"),
    (b"[1, 2, 3]", "malformed envelope"),
    (json.dumps({"kind": "martian", "item": {}}).encode(), "unparseable item"),
    (json.dumps(envelope("usgs", {"id": "x", "properties": {}, "geometry": {}})).encode(), "unparseable item"),
    (json.dumps(envelope("nws", {"properties": {"id": "x", "event": "Flood Warning", "status": "Actual",
                                                "affectedZones": None, "geocode": "oops"}}, "nws_alerts")).encode(),
     "unparseable item"),
])
def test_bad_messages_go_to_dead_letters_and_processing_continues(processor, store, message, reason):
    summary = processor.handle(message, location="hazards.raw/0/7")
    assert summary["rejected"] == 1
    dl = store.read("SELECT * FROM dead_letters")
    assert len(dl) == 1 and dl[0]["reason"].startswith(reason) and dl[0]["location"] == "hazards.raw/0/7"
    processor.handle(raw_log()[0])
    assert store.read("SELECT COUNT(*) n FROM source_events")[0]["n"] == 1


def test_schema_violation_is_rejected_not_stored(processor, store, monkeypatch):
    import src.hazard_enrichment as he
    original = he.map_usgs

    def broken(item, ctx):
        events = original(item, ctx)
        events[0]["lat"], events[0]["lon"] = 0.0, 0.0
        return events
    monkeypatch.setitem(he.MAPPERS, "usgs", broken)
    summary = processor.handle(envelope("usgs", fixture("usgs_feed.json")["features"][2], "usgs_quakes"))
    assert summary["rejected"] == 1 and store.read("SELECT COUNT(*) n FROM source_events")[0]["n"] == 0
    assert "0,0 is not a location" in store.read("SELECT reason FROM dead_letters")[0]["reason"]


class FakeConsumer:
    def __init__(self, batches, stop):
        self.batches = list(batches)
        self.stop = stop
        self.commits = 0
        self.seeks = []

    def poll(self, timeout_ms=0, max_records=0):
        if not self.batches:
            self.stop.set()
            return {}
        return self.batches.pop(0)

    def seek(self, tp, offset):
        self.seeks.append((tp, offset))

    def commit(self):
        self.commits += 1

    def assignment(self):
        return set()


def test_offsets_are_committed_only_after_the_batch_is_stored(processor, store):
    stop = threading.Event()
    tp = TP("hazards.raw", 0)
    log = raw_log()
    batch = {tp: [Record(m, 0, i) for i, m in enumerate(log[:3])]}
    consumer = FakeConsumer([batch], stop)
    consume_kafka(processor, consumer, stop)
    assert consumer.commits == 1 and consumer.seeks == []
    assert store.read("SELECT COUNT(*) n FROM source_events")[0]["n"] >= 2


def test_database_failure_rewinds_instead_of_committing(processor, store, monkeypatch):
    stop = threading.Event()
    tp = TP("hazards.raw", 0)
    log = raw_log()
    batch = {tp: [Record(m, 0, 40 + i) for i, m in enumerate(log[:3])]}
    calls = {"n": 0}
    real = store.insert_event

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return real(*args, **kwargs)
    monkeypatch.setattr(store, "insert_event", flaky)
    consumer = FakeConsumer([batch, copy.deepcopy(batch)], stop)
    monkeypatch.setattr(stop, "wait", lambda t: None)
    consume_kafka(processor, consumer, stop)
    assert consumer.seeks == [(tp, 40)]
    assert consumer.commits == 1
    assert store.read("SELECT COUNT(*) n FROM source_events")[0]["n"] >= 2


def test_a_poison_event_does_not_block_the_partition(processor, store, monkeypatch):
    stop = threading.Event()
    tp = TP("hazards.raw", 0)
    monkeypatch.setattr(processor.engine, "process", lambda *a, **k: (_ for _ in ()).throw(KeyError("boom")))
    consumer = FakeConsumer([{tp: [Record(raw_log()[0], 0, 0)]}], stop)
    consume_kafka(processor, consumer, stop)
    assert consumer.commits == 1
    assert "processing" in store.read("SELECT reason FROM dead_letters")[0]["reason"]


def test_legacy_v1_envelopes_still_process(processor, store):
    quake = fixture("usgs_feed.json")["features"][2]
    processor.handle(json.dumps({"source": "usgs", "kind": "usgs", "item": quake}).encode(),
                     received_at="2026-10-15T16:00:00+00:00")
    assert store.read("SELECT feed, first_observed FROM source_events")[0] == \
        {"feed": "usgs", "first_observed": "2026-10-15T16:00:00+00:00"}


def test_latency_metric_ignores_backlog_published_before_start(processor, clock):
    from datetime import datetime, timezone
    from src.metrics import availability_latency
    metric = availability_latency.labels("usgs")
    before = metric._sum.get()
    processor.started_at = datetime(2026, 10, 15, 16, 55, tzinfo=timezone.utc)
    backlog = fixture("usgs_feed.json")["features"][2]
    processor.handle(envelope("usgs", backlog, "usgs_quakes", fetched_at="2026-10-15T17:00:00+00:00"))
    assert metric._sum.get() == before
    fresh = json.loads(json.dumps(backlog))
    fresh["id"] = "us-fresh"
    published_ms = int(datetime(2026, 10, 15, 16, 59, 30, tzinfo=timezone.utc).timestamp() * 1000)
    fresh["properties"].update(time=published_ms, updated=published_ms)
    processor.handle(envelope("usgs", fresh, "usgs_quakes", fetched_at="2026-10-15T17:00:00+00:00"))
    assert metric._sum.get() - before == 30.0
