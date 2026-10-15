import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import geo

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS source_events (
    event_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    incident_id TEXT,
    source TEXT NOT NULL,
    feed TEXT NOT NULL,
    category TEXT NOT NULL,
    type TEXT NOT NULL,
    severity INTEGER NOT NULL,
    title TEXT NOT NULL,
    event_time TEXT NOT NULL,
    published_at TEXT,
    expires_at TEXT,
    first_observed TEXT NOT NULL,
    stored_at TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    location_confidence TEXT NOT NULL,
    lat REAL, lon REAL, h3 TEXT,
    min_lat REAL, min_lon REAL, max_lat REAL, max_lon REAL,
    series_key TEXT,
    payload TEXT NOT NULL,
    PRIMARY KEY (event_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_events_content ON source_events(event_id, content_hash);
CREATE INDEX IF NOT EXISTS ix_events_incident ON source_events(incident_id);
CREATE INDEX IF NOT EXISTS ix_events_time ON source_events(event_time);
CREATE INDEX IF NOT EXISTS ix_events_area ON source_events(min_lat, max_lat, min_lon, max_lon);

CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    clear_reason TEXT,
    category TEXT NOT NULL,
    type TEXT NOT NULL,
    subtype TEXT,
    severity INTEGER NOT NULL,
    verification TEXT NOT NULL,
    location_confidence TEXT NOT NULL,
    location_precision TEXT,
    place TEXT, region TEXT,
    lat REAL, lon REAL, h3 TEXT,
    min_lat REAL, min_lon REAL, max_lat REAL, max_lon REAL,
    geometry TEXT,
    road TEXT, direction TEXT,
    title TEXT NOT NULL,
    description TEXT,
    sources TEXT NOT NULL,
    evidence_count INTEGER NOT NULL,
    event_time TEXT NOT NULL,
    first_observed TEXT NOT NULL,
    last_updated TEXT NOT NULL,
    expires_at TEXT,
    cleared_at TEXT,
    series_key TEXT,
    report_hash TEXT,
    schema_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_incidents_state ON incidents(state, event_time);
CREATE INDEX IF NOT EXISTS ix_incidents_time ON incidents(event_time);
CREATE INDEX IF NOT EXISTS ix_incidents_corr ON incidents(category, state, h3);
CREATE INDEX IF NOT EXISTS ix_incidents_series ON incidents(series_key, state);
CREATE INDEX IF NOT EXISTS ix_incidents_expiry ON incidents(state, expires_at);

CREATE TABLE IF NOT EXISTS incident_history (
    incident_id TEXT NOT NULL,
    at TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    event_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_history_incident ON incident_history(incident_id);

CREATE TABLE IF NOT EXISTS cache (
    provider TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, fetched_at REAL NOT NULL,
    PRIMARY KEY (provider, key)
);

CREATE TABLE IF NOT EXISTS feed_status (
    feed TEXT PRIMARY KEY,
    kind TEXT,
    last_attempt TEXT,
    last_success TEXT,
    last_error TEXT,
    last_error_category TEXT,
    last_error_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    items_last_poll INTEGER,
    published_last_poll INTEGER,
    poll_interval INTEGER
);

CREATE TABLE IF NOT EXISTS dead_letters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    source TEXT,
    reason TEXT NOT NULL,
    location TEXT,
    payload TEXT NOT NULL
);
"""

INCIDENT_COLUMNS = (
    "incident_id", "state", "clear_reason", "category", "type", "subtype", "severity", "verification",
    "location_confidence", "location_precision", "place", "region", "lat", "lon", "h3",
    "min_lat", "min_lon", "max_lat", "max_lon", "geometry", "road", "direction", "title", "description",
    "sources", "evidence_count", "event_time", "first_observed", "last_updated", "expires_at", "cleared_at",
    "series_key", "report_hash", "schema_version",
)


class Store:
    def __init__(self, path):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._local = threading.local()
        self._shared = None
        if path == ":memory:":
            self._shared = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
            self._shared.row_factory = sqlite3.Row
            self._lock = threading.RLock()
        self.connect().executescript(SCHEMA)
        with self.transaction() as con:
            con.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', '2')")

    def connect(self):
        if self._shared is not None:
            return self._shared
        con = getattr(self._local, "con", None)
        if con is None:
            con = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA busy_timeout=30000")
            self._local.con = con
        return con

    @contextmanager
    def transaction(self):
        con = self.connect()
        lock = self._lock if self._shared is not None else None
        if lock:
            lock.acquire()
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
            except BaseException:
                con.execute("ROLLBACK")
                raise
            con.execute("COMMIT")
        finally:
            if lock:
                lock.release()

    def read(self, sql, params=()):
        con = self.connect()
        if self._shared is not None:
            with self._lock:
                return [dict(r) for r in con.execute(sql, params).fetchall()]
        return [dict(r) for r in con.execute(sql, params).fetchall()]

    # ---- source events ----

    @staticmethod
    def latest_version(con, event_id):
        row = con.execute(
            "SELECT * FROM source_events WHERE event_id = ? ORDER BY version DESC LIMIT 1", (event_id,)
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def insert_event(con, event, content_hash, version, incident_id, first_observed, stored_at):
        box = geo.bbox(event.get("geometry")) if event.get("geometry") else None
        if box is None and event.get("lat") is not None:
            box = (event["lat"], event["lon"], event["lat"], event["lon"])
        con.execute(
            "INSERT INTO source_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event["event_id"], version, content_hash, incident_id, event["source"], event["feed"],
                event["category"], event["type"], event["severity"], event["title"], event["event_time"],
                event.get("published_at"), event.get("expires_at"), first_observed, stored_at, stored_at,
                event["location"]["confidence"], event.get("lat"), event.get("lon"), event.get("h3"),
                *(box or (None, None, None, None)), event.get("series_key"),
                json.dumps(event, sort_keys=True, separators=(",", ":")),
            ),
        )

    @staticmethod
    def touch_event(con, event_id, version, last_seen, expires_at):
        con.execute(
            "UPDATE source_events SET last_seen = ?, expires_at = CASE WHEN ? > COALESCE(expires_at, '') "
            "THEN ? ELSE expires_at END WHERE event_id = ? AND version = ?",
            (last_seen, expires_at or "", expires_at, event_id, version),
        )

    @staticmethod
    def evidence(con, incident_id):
        rows = con.execute(
            "SELECT e.* FROM source_events e JOIN (SELECT event_id, MAX(version) v FROM source_events "
            "WHERE incident_id = ? GROUP BY event_id) last ON e.event_id = last.event_id AND e.version = last.v "
            "ORDER BY e.event_time, e.event_id",
            (incident_id,),
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["event"] = json.loads(item.pop("payload"))
            out.append(item)
        return out

    @staticmethod
    def incidents_for_events(con, event_ids):
        if not event_ids:
            return []
        marks = ",".join("?" for _ in event_ids)
        rows = con.execute(
            f"SELECT DISTINCT incident_id FROM source_events WHERE event_id IN ({marks}) AND incident_id IS NOT NULL",
            tuple(event_ids),
        ).fetchall()
        return [r[0] for r in rows]

    # ---- incidents ----

    @staticmethod
    def get_incident(con, incident_id):
        row = con.execute("SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def save_incident(con, incident):
        values = [incident.get(c) for c in INCIDENT_COLUMNS]
        marks = ",".join("?" for _ in INCIDENT_COLUMNS)
        updates = ",".join(f"{c}=excluded.{c}" for c in INCIDENT_COLUMNS[1:])
        con.execute(
            f"INSERT INTO incidents ({','.join(INCIDENT_COLUMNS)}) VALUES ({marks}) "
            f"ON CONFLICT(incident_id) DO UPDATE SET {updates}",
            values,
        )

    @staticmethod
    def add_history(con, incident_id, at, from_state, to_state, reason, event_id=None):
        con.execute("INSERT INTO incident_history VALUES (?,?,?,?,?,?)",
                    (incident_id, at, from_state, to_state, reason, event_id))

    @staticmethod
    def correlation_candidates(con, category, cells, since, until):
        marks = ",".join("?" for _ in cells)
        rows = con.execute(
            f"SELECT * FROM incidents WHERE category = ? AND state != 'cleared' AND h3 IN ({marks}) "
            f"AND event_time BETWEEN ? AND ?",
            (category, *cells, since, until),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def open_series(con, series_key):
        row = con.execute(
            "SELECT * FROM incidents WHERE series_key = ? AND state != 'cleared' ORDER BY event_time DESC LIMIT 1",
            (series_key,),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def expired(con, now):
        rows = con.execute(
            "SELECT * FROM incidents WHERE state != 'cleared' AND expires_at IS NOT NULL AND expires_at <= ?",
            (now,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def open_for_source(con, source):
        rows = con.execute(
            "SELECT DISTINCT i.* FROM incidents i JOIN source_events e ON e.incident_id = i.incident_id "
            "WHERE i.state != 'cleared' AND e.source = ?",
            (source,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def withdrawn_for_source(con, source):
        rows = con.execute(
            "SELECT DISTINCT i.* FROM incidents i JOIN source_events e ON e.incident_id = i.incident_id "
            "WHERE i.state = 'cleared' AND i.clear_reason = 'source_withdrawn' AND e.source = ?",
            (source,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- caches ----

    def cache_get(self, provider, key, ttl):
        rows = self.read("SELECT value, fetched_at FROM cache WHERE provider = ? AND key = ?", (provider, key))
        if not rows or time.time() - rows[0]["fetched_at"] > ttl:
            return None
        return json.loads(rows[0]["value"])

    def cache_put(self, provider, key, value):
        with self.transaction() as con:
            con.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?)",
                        (provider, key, json.dumps(value), time.time()))

    def zone_geometry(self, zone_id):
        return self.cache_get("nws_zone", zone_id, 30 * 86400) or None

    # ---- feed health ----

    def record_poll(self, feed, kind, poll_interval, at, ok, items=None, published=None,
                    error_category=None, error=None):
        with self.transaction() as con:
            con.execute("INSERT OR IGNORE INTO feed_status (feed, kind, poll_interval) VALUES (?,?,?)",
                        (feed, kind, poll_interval))
            if ok:
                con.execute(
                    "UPDATE feed_status SET kind=?, poll_interval=?, last_attempt=?, last_success=?, "
                    "consecutive_failures=0, items_last_poll=?, published_last_poll=? WHERE feed=?",
                    (kind, poll_interval, at, at, items, published, feed),
                )
            else:
                con.execute(
                    "UPDATE feed_status SET kind=?, poll_interval=?, last_attempt=?, last_error=?, "
                    "last_error_category=?, last_error_at=?, consecutive_failures=consecutive_failures+1 "
                    "WHERE feed=?",
                    (kind, poll_interval, at, (error or "")[:500], error_category, at, feed),
                )

    def feed_status(self):
        return self.read("SELECT * FROM feed_status ORDER BY feed")

    def dead_letter(self, received_at, source, reason, location, payload):
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", "replace")
        with self.transaction() as con:
            con.execute("INSERT INTO dead_letters (received_at, source, reason, location, payload) VALUES (?,?,?,?,?)",
                        (received_at, source, reason[:500], location, payload[:200000]))

    # ---- read models ----

    def counts(self):
        rows = self.read("SELECT state, category, COUNT(*) n FROM incidents GROUP BY state, category")
        events = self.read("SELECT COUNT(*) n, COUNT(DISTINCT event_id) d FROM source_events")[0]
        dead = self.read("SELECT COUNT(*) n FROM dead_letters")[0]["n"]
        return {"incidents": rows, "event_versions": events["n"], "events": events["d"], "dead_letters": dead}

    def query_incidents(self, state="active", categories=None, types=None, since=None, until=None, search=None,
                        bbox=None, location="any", limit=500, after=None, geometry=False, regions=None,
                        min_severity=None, overlap=False):
        """overlap=True keeps incidents active at any time in [since, until] instead of starting in it."""
        where, params = [], []
        if state == "active":
            where.append("state != 'cleared'")
        elif state == "cleared":
            where.append("state = 'cleared'")
        if categories:
            where.append(f"category IN ({','.join('?' for _ in categories)})")
            params += list(categories)
        if types:
            where.append(f"type IN ({','.join('?' for _ in types)})")
            params += list(types)
        if since:
            where.append("COALESCE(cleared_at, expires_at, '9999-12-31') >= ?" if overlap else "event_time >= ?")
            params.append(since)
        if until:
            where.append("event_time <= ?")
            params.append(until)
        if regions:
            where.append("(" + " OR ".join("(',' || COALESCE(region, '') || ',') LIKE ?" for _ in regions) + ")")
            params += [f"%,{r},%" for r in regions]
        if min_severity:
            where.append("severity >= ?")
            params.append(int(min_severity))
        if search:
            where.append("(LOWER(title) LIKE ? OR LOWER(COALESCE(place,'')) LIKE ? OR LOWER(COALESCE(road,'')) LIKE ? "
                         "OR sources LIKE ? OR type LIKE ?)")
            params += [f"%{search.lower()}%"] * 5
        if bbox:
            min_lon, min_lat, max_lon, max_lat = bbox
            where.append("max_lat >= ? AND min_lat <= ? AND max_lon >= ? AND min_lon <= ?")
            params += [min_lat, max_lat, min_lon, max_lon]
        if location == "verified":
            where.append("location_confidence IN ('source_point','source_area')")
        elif location == "mappable":
            where.append("location_confidence IN ('source_point','source_area','geocoded_high','geocoded_medium')")
        if after:
            where.append("(event_time < ? OR (event_time = ? AND incident_id < ?))")
            params += [after[0], after[0], after[1]]
        cols = "*" if geometry else ", ".join(c for c in INCIDENT_COLUMNS if c != "geometry")
        sql = f"SELECT {cols} FROM incidents"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY event_time DESC, incident_id DESC LIMIT ?"
        return self.read(sql, (*params, int(limit)))

    def query_events(self, since=None, until=None, search=None, sources=None, limit=500):
        where, params = ["version = (SELECT MAX(version) FROM source_events s WHERE s.event_id = source_events.event_id)"], []
        if since:
            where.append("event_time >= ?")
            params.append(since)
        if until:
            where.append("event_time <= ?")
            params.append(until)
        if sources:
            where.append(f"source IN ({','.join('?' for _ in sources)})")
            params += list(sources)
        if search:
            where.append("LOWER(title) LIKE ?")
            params.append(f"%{search.lower()}%")
        sql = ("SELECT event_id, version, incident_id, source, feed, category, type, severity, title, event_time, "
               "published_at, first_observed, last_seen, expires_at, location_confidence, lat, lon FROM source_events "
               "WHERE " + " AND ".join(where) + " ORDER BY event_time DESC LIMIT ?")
        return self.read(sql, (*params, int(limit)))

    def incident_detail(self, incident_id):
        rows = self.read("SELECT * FROM incidents WHERE incident_id = ?", (incident_id,))
        if not rows:
            return None
        con = self.connect()
        evidence = self.evidence(con, incident_id)
        history = self.read("SELECT at, from_state, to_state, reason, event_id FROM incident_history "
                            "WHERE incident_id = ? ORDER BY rowid", (incident_id,))
        return {"incident": rows[0], "evidence": evidence, "history": history}

    def areas_containing(self, lat, lon, start, end, sources=("nws",)):
        """Every version of an area event whose polygon contains the point and whose validity overlaps [start, end]."""
        marks = ",".join("?" for _ in sources)
        rows = self.read(
            f"SELECT event_id, version, incident_id, source, type, severity, title, event_time, published_at, "
            f"expires_at, payload FROM source_events WHERE source IN ({marks}) AND min_lat <= ? AND max_lat >= ? "
            f"AND min_lon <= ? AND max_lon >= ? AND event_time <= ? AND (expires_at IS NULL OR expires_at >= ?) "
            f"ORDER BY event_time, event_id, version",
            (*sources, lat, lat, lon, lon, end, start),
        )
        hits = []
        for row in rows:
            event = json.loads(row.pop("payload"))
            if event.get("geometry") and geo.contains(event["geometry"], lat, lon):
                row["source_url"] = event.get("source_url")
                row["attributes"] = {k: event["attributes"].get(k) for k in ("event", "certainty", "urgency", "onset", "ends")}
                row["area"] = event["location"].get("place")
                hits.append(row)
        return hits

    def active_areas(self, limit=300):
        return self.read("SELECT incident_id, type, severity, title, place, geometry FROM incidents "
                         "WHERE state != 'cleared' AND location_confidence = 'source_area' AND geometry IS NOT NULL "
                         "ORDER BY severity DESC, event_time DESC LIMIT ?", (limit,))

    def dead_letter_sample(self, limit=10):
        return self.read("SELECT id, received_at, source, reason, location FROM dead_letters ORDER BY id DESC LIMIT ?",
                         (limit,))
