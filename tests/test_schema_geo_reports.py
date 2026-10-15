import copy
import csv
import io
import os

import pytest
import requests

from src import geo
from src.hazard_enrichment import normalize
from src.ingestion.base import BaseFeed, FeedError
from src.reports import CSV_COLUMNS, incidents_csv
from src.schema import SchemaError, content_hash, validate
from tests.conftest import Collector, FakeSession, envelope, fixture


@pytest.fixture
def quake_event(processor):
    events, _ = normalize(envelope("usgs", fixture("usgs_feed.json")["features"][2], "usgs_quakes"), processor.context)
    return events[0]


# ---------- schema ----------

def test_valid_event_passes(quake_event):
    assert validate(quake_event) is quake_event


@pytest.mark.parametrize("mutate,message", [
    (lambda e: e.pop("event_time"), "missing field event_time"),
    (lambda e: e.update(event_time="2026-10-15T12:00:00"), "timestamp without timezone"),
    (lambda e: e.update(lat=0.0, lon=0.0), "0,0 is not a location"),
    (lambda e: e.update(lat=95.0), "out of range"),
    (lambda e: e.pop("lon"), "set together"),
    (lambda e: e.update(severity=7), "severity"),
    (lambda e: e.update(category="vibes"), "category"),
    (lambda e: e.update(schema_version=1), "schema_version"),
    (lambda e: e.update(event_id="other:123"), "prefixed"),
    (lambda e: e.update(confidence=0.9), "unknown fields"),
    (lambda e: e["location"].update(confidence="geocoded_low"), "not allowed with geocoded_low"),
    (lambda e: e["location"].update(confidence="probably"), "location confidence"),
])
def test_invalid_events_are_rejected(quake_event, mutate, message):
    e = copy.deepcopy(quake_event)
    mutate(e)
    with pytest.raises(SchemaError, match=message):
        validate(e)


def test_content_hash_ignores_observation_bookkeeping(quake_event):
    later = copy.deepcopy(quake_event)
    later["expires_at"] = "2030-01-01T00:00:00+00:00"
    later["published_at"] = "2030-01-01T00:00:00+00:00"
    assert content_hash(later) == content_hash(quake_event)
    later["severity"] = 3
    assert content_hash(later) != content_hash(quake_event)


# ---------- geometry ----------

SQUARE_WITH_HOLE = {"type": "Polygon", "coordinates": [
    [[-100, 30], [-90, 30], [-90, 40], [-100, 40], [-100, 30]],
    [[-96, 34], [-94, 34], [-94, 36], [-96, 36], [-96, 34]],
]}


def test_point_in_polygon_respects_holes():
    assert geo.contains(SQUARE_WITH_HOLE, 32, -98)
    assert not geo.contains(SQUARE_WITH_HOLE, 35, -95)
    assert not geo.contains(SQUARE_WITH_HOLE, 45, -95)


def test_multipolygon_and_real_zone_geometry():
    zone = fixture("nws_zones/SDZ063.json")["geometry"]
    min_lat, min_lon, max_lat, max_lon = geo.bbox(zone)
    multi = geo.merge([SQUARE_WITH_HOLE, zone])
    assert multi["type"] == "MultiPolygon"
    inside = [(lat / 100, lon / 100) for lat in range(int(min_lat * 100), int(max_lat * 100), 3)
              for lon in range(int(min_lon * 100), int(max_lon * 100), 3) if geo.contains(zone, lat / 100, lon / 100)]
    assert inside, "a grid over the zone bbox should hit the zone"
    lat, lon = inside[len(inside) // 2]
    assert geo.contains(multi, lat, lon) and geo.contains(multi, 32, -98)
    assert not geo.contains(zone, max_lat + 1, max_lon + 1)


def test_centroid_is_not_used_for_area_events(processor):
    item = next(f for f in fixture("nws_active.json")["features"] if f["properties"]["event"] == "Wind Advisory"
                and f["properties"]["messageType"] == "Update")
    e = normalize(envelope("nws", item, "nws_alerts"), processor.context)[0][0]
    assert "lat" not in e and "h3" not in e and e["geometry"]


def test_simplify_keeps_shape_closed_and_smaller():
    zone = fixture("nws_zones/NMZ028.json")["geometry"]
    simple = geo.simplify(zone, 0.0005)
    before = sum(len(r) for p in geo.polygons(zone) for r in p)
    after = sum(len(r) for p in geo.polygons(simple) for r in p)
    assert after < before
    for poly in geo.polygons(simple):
        assert poly[0][0] == poly[0][-1]


def test_h3_neighbours_and_distance():
    c = geo.cell(40.76, -112.08)
    assert len(geo.neighbors(c, 1)) == 7
    assert geo.haversine_km(40.0, -100.0, 41.0, -100.0) == pytest.approx(111.2, abs=0.5)


# ---------- CSV and reports ----------

def test_csv_header_matches_rows_and_escapes_text():
    rows = [{c: None for c in CSV_COLUMNS} | {"incident_id": "inc_1", "title": 'Crash, "big", on I-80\nsecond line',
                                               "severity": 3}]
    text = incidents_csv(rows)
    parsed = list(csv.reader(io.StringIO(text)))
    assert parsed[0] == list(CSV_COLUMNS)
    assert all(len(r) == len(CSV_COLUMNS) for r in parsed)
    assert parsed[1][CSV_COLUMNS.index("title")] == 'Crash, "big", on I-80\nsecond line'


def test_export_is_regenerated_not_appended(processor, store):
    from tests.test_processor import raw_log
    for m in raw_log():
        processor.handle(m)
    processor.reports.export_csv()
    processor.reports.export_csv()
    with open(processor.reports.csv_path, newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == list(CSV_COLUMNS)
    assert len(rows) - 1 == store.read("SELECT COUNT(*) n FROM incidents")[0]["n"]
    ids = [r[0] for r in rows[1:]]
    assert len(ids) == len(set(ids))


def test_incident_report_explains_provenance(processor, store):
    processor.handle(envelope("usgs", fixture("usgs_feed.json")["features"][2], "usgs_quakes"))
    incident_id = store.read("SELECT incident_id FROM incidents")[0]["incident_id"]
    with open(os.path.join(processor.reports.dir, f"{incident_id}.txt")) as f:
        text = f.read()
    assert "Verification: official" in text and "Evidence (1 source records)" in text
    assert "location basis: method=epicenter" in text and "url=https://earthquake.usgs.gov" in text


# ---------- feed base ----------

class ExplodingFeed(BaseFeed):
    kind = "usgs"

    def __init__(self, error, **kw):
        super().__init__("exploding", {"poll_interval": 60}, **kw)
        self.error = error

    def fetch(self):
        raise self.error

    def parse(self, raw):
        return raw


def test_timeouts_and_connection_errors_are_categorised(store):
    for exc, category in [(requests.Timeout("slow"), "timeout"), (requests.ConnectionError("down"), "connection")]:
        feed = ExplodingFeed(None, store=store, session=FakeSession({"https://x": exc}))
        with pytest.raises(FeedError) as err:
            feed.get("https://x")
        assert err.value.category == category


def test_unexpected_exceptions_are_logged_and_counted(store, caplog):
    feed = ExplodingFeed(RuntimeError("bug"), store=store)
    result = feed.poll_once(Collector())
    assert result["category"] == "internal"
    assert any("feed poll crashed" in r.message for r in caplog.records)
    assert store.feed_status()[0]["last_error_category"] == "internal"


def test_failures_back_off_and_recover(store):
    feed = ExplodingFeed(FeedError("http_503", "busy"), store=store)
    feed.poll_once(Collector())
    feed.poll_once(Collector())
    assert feed.failures == 2 and feed.next_delay() >= 60 * 4 * 0.9
    assert store.feed_status()[0]["consecutive_failures"] == 2


class ListFeed(BaseFeed):
    kind = "usgs"

    def fetch(self):
        return [{"id": "a"}, {"id": "b"}]

    def parse(self, raw):
        return raw

    def item_id(self, item):
        return item["id"]


def test_broker_failure_is_a_failed_poll_and_items_are_retried(store):
    feed = ListFeed("list", {"poll_interval": 60}, store=store)

    def broken(*args):
        raise ConnectionError("broker down")
    assert feed.poll_once(broken)["category"] == "publish"
    out = Collector()
    assert feed.poll_once(out)["published"] == 2
