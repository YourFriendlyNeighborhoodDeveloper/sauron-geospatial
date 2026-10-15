import json
import logging
import queue
import sqlite3
import time
from datetime import datetime, timezone

from .hazard_enrichment import Context, normalize
from .incidents import IncidentEngine
from .metrics import (
    availability_latency, consumer_lag, dead_letters, duplicates, events_filtered, events_normalized,
    events_rejected, incidents_active, processing_errors, processing_seconds,
)
from .schema import SchemaError, content_hash, parse_time, utc_iso, validate

log = logging.getLogger(__name__)

LIVE_WINDOW_SECS = 600


def utcnow():
    return datetime.now(timezone.utc)


class Processor:
    def __init__(self, store, cfg, geocoder=None, reports=None, clock=utcnow):
        self.store = store
        self.cfg = cfg
        self.geocoder = geocoder
        self.reports = reports
        self.clock = clock
        self.engine = IncidentEngine(store, cfg)
        self._contexts = {}
        self.started_at = clock()

    def context(self, feed):
        if feed not in self._contexts:
            self._contexts[feed] = Context(feed, self.cfg, self.geocoder, self.store.zone_geometry)
        return self._contexts[feed]

    def handle(self, message, received_at=None, location=None):
        """Process one raw envelope. Returns a summary; never raises for bad input."""
        started = time.perf_counter()
        now = utc_iso(self.clock())
        received_at = received_at or now
        summary = {"stored": 0, "duplicates": 0, "filtered": 0, "rejected": 0, "changed": []}
        try:
            envelope = json.loads(message) if isinstance(message, (bytes, str)) else message
            if not isinstance(envelope, dict):
                raise ValueError("envelope is not an object")
        except ValueError as exc:
            self._dead_letter(received_at, None, f"malformed envelope: {exc}", location, message)
            events_rejected.labels("unknown", "malformed_envelope").inc()
            summary["rejected"] += 1
            return summary

        source = envelope.get("kind") or envelope.get("source") or "unknown"
        observed = envelope.get("fetched_at") or received_at
        try:
            if "snapshot" in envelope:
                active = (envelope["snapshot"] or {}).get("active_ids") or []
                with self.store.transaction() as con:
                    cleared = self.engine.withdraw(con, source, active, utc_iso(parse_time(observed)))
                summary["changed"].extend(cleared)
                self._write_reports(cleared)
                return summary

            events, skipped = normalize(envelope, self.context)
        except sqlite3.Error:
            raise
        except Exception as exc:
            self._dead_letter(received_at, source, f"unparseable item: {exc!r}", location, envelope)
            events_rejected.labels(source, "unparseable_item").inc()
            summary["rejected"] += 1
            return summary

        for reason in skipped:
            events_filtered.labels(source, reason).inc()
            summary["filtered"] += 1

        for event in events:
            try:
                validate(event)
            except SchemaError as exc:
                log.warning("event rejected", extra={"source": source, "event_id": event.get("event_id"),
                                                     "error_category": "schema", "error": str(exc)})
                events_rejected.labels(source, "schema").inc()
                self._dead_letter(received_at, source, f"schema: {exc}", location, event)
                summary["rejected"] += 1
                continue
            events_normalized.labels(source).inc()
            try:
                changed = self._store_event(event, observed, now)
            except sqlite3.Error:
                raise
            except Exception as exc:
                processing_errors.labels("incident").inc()
                log.exception("event processing failed", extra={"source": source, "event_id": event["event_id"]})
                self._dead_letter(received_at, source, f"processing: {exc!r}", location, event)
                summary["rejected"] += 1
                continue
            if changed is None:
                summary["duplicates"] += 1
            else:
                summary["stored"] += 1
                summary["changed"].append(changed)

        self._write_reports(summary["changed"])
        processing_seconds.observe(time.perf_counter() - started)
        return summary

    def _store_event(self, event, observed, now):
        digest = content_hash(event)
        first_observed = utc_iso(parse_time(observed))
        with self.store.transaction() as con:
            prior = self.store.latest_version(con, event["event_id"])
            if prior and prior["content_hash"] == digest:
                self.store.touch_event(con, event["event_id"], prior["version"], first_observed, event.get("expires_at"))
                if prior["incident_id"]:
                    self.engine.touch(con, prior["incident_id"], event.get("expires_at"), now)
                duplicates.labels("store").inc()
                return None
            if prior is None and event.get("series_key"):
                current = self.store.open_series(con, event["series_key"])
                if current and current["severity"] == event["severity"]:
                    self.engine.touch(con, current["incident_id"], event.get("expires_at"), now)
                    duplicates.labels("reobservation").inc()
                    return None
            version = prior["version"] + 1 if prior else 1
            incident = self.engine.process(con, event, version, digest, first_observed, now, now)
        if version == 1:
            published = parse_time(event.get("published_at") or event["event_time"])
            age = (parse_time(now) - parse_time(first_observed)).total_seconds()
            if age <= LIVE_WINDOW_SECS and published >= self.started_at:
                availability_latency.labels(event["source"]).observe(
                    max(0.0, (parse_time(now) - published).total_seconds()))
        return incident["incident_id"]

    def expire(self):
        now = utc_iso(self.clock())
        with self.store.transaction() as con:
            cleared = self.engine.expire(con, now)
        self._write_reports(cleared)
        return cleared

    def refresh_gauges(self):
        rows = self.store.read("SELECT category, COUNT(*) n FROM incidents WHERE state != 'cleared' GROUP BY category")
        counts = {r["category"]: r["n"] for r in rows}
        for category in ("roadway", "weather", "seismic"):
            incidents_active.labels(category).set(counts.get(category, 0))

    def _write_reports(self, incident_ids):
        if not self.reports:
            return
        for incident_id in dict.fromkeys(incident_ids):
            try:
                self.reports.write_incident(incident_id)
            except OSError as exc:
                processing_errors.labels("report").inc()
                log.error("report write failed", extra={"incident_id": incident_id, "error": str(exc)})

    def _dead_letter(self, received_at, source, reason, location, payload):
        dead_letters.labels(source or "unknown").inc()
        if not isinstance(payload, (bytes, str)):
            payload = json.dumps(payload, default=str)
        self.store.dead_letter(received_at, source, reason, location, payload)


def consume_kafka(processor, consumer, stop, lag_every=30):
    """At-least-once: offsets are committed only after every record in the batch is stored."""
    last_lag = 0.0
    while not stop.is_set():
        batch = consumer.poll(timeout_ms=1000, max_records=200)
        if not batch:
            last_lag = _report_lag(consumer, last_lag, lag_every)
            continue
        try:
            for tp, records in batch.items():
                for record in records:
                    received = utc_iso(datetime.fromtimestamp(record.timestamp / 1000, tz=timezone.utc)) \
                        if record.timestamp and record.timestamp > 0 else None
                    processor.handle(record.value, received, f"{tp.topic}/{tp.partition}/{record.offset}")
        except Exception as exc:
            processing_errors.labels("store").inc()
            log.exception("batch failed; rewinding to last committed offsets", extra={"error": str(exc)})
            for tp, records in batch.items():
                consumer.seek(tp, records[0].offset)
            stop.wait(5)
            continue
        consumer.commit()
        last_lag = _report_lag(consumer, last_lag, lag_every)


def _report_lag(consumer, last, every):
    if time.monotonic() - last < every:
        return last
    try:
        assigned = consumer.assignment()
        if assigned:
            ends = consumer.end_offsets(list(assigned))
            for tp in assigned:
                consumer_lag.labels(str(tp.partition)).set(max(0, ends[tp] - consumer.position(tp)))
    except Exception as exc:
        log.warning("lag check failed", extra={"error": str(exc)})
    return time.monotonic()


def consume_queue(processor, q, stop):
    while not stop.is_set():
        try:
            message = q.get(timeout=1)
        except queue.Empty:
            continue
        try:
            processor.handle(message)
        except Exception:
            processing_errors.labels("store").inc()
            log.exception("local message failed")
