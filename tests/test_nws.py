import json

import pytest

from src.hazard_enrichment import Context, map_nws, normalize
from src.ingestion.base import FeedError
from src.ingestion.nws_alerts import NWSFeed
from src.schema import parse_time, validate
from tests.conftest import Collector, FakeResponse, FakeSession, envelope, fixture

NWS = "https://api.weather.gov/alerts/active"


def make_feed(cfg, store=None, routes=None):
    session = FakeSession(routes or {})
    feed = NWSFeed("nws_alerts", cfg["feeds"]["nws_alerts"], store=store, session=session)
    return feed, session


def by_event(features, name, message_type=None):
    return next(f for f in features if f["properties"]["event"] == name
                and (message_type is None or f["properties"]["messageType"] == message_type))


def test_response_is_parsed_once_and_every_alert_survives(cfg):
    data = fixture("nws_active.json")
    feed, session = make_feed(cfg, routes={NWS: FakeResponse(body=data)})
    items = feed.parse(feed.fetch())
    assert len(items) == len(data["features"]) == 6
    assert session.calls[0]["params"] == {"status": "actual", "message_type": "alert,update", "region_type": "land"}
    assert session.calls[0]["headers"]["Accept"] == "application/geo+json"


def test_one_nationwide_request_replaces_per_state_polling(cfg, store):
    feed, session = make_feed(cfg, store, {NWS: FakeResponse(body={"type": "FeatureCollection", "features": []})})
    feed.poll_once(Collector())
    assert [c["url"] for c in session.calls] == [NWS]


def test_legitimately_empty_feed_is_a_successful_poll(cfg, store):
    feed, _ = make_feed(cfg, store, {NWS: FakeResponse(body={"type": "FeatureCollection", "features": []})})
    out = Collector()
    result = feed.poll_once(out)
    assert result == {"outcome": "success", "items": 0, "published": 0}
    status = store.feed_status()[0]
    assert status["last_success"] and status["consecutive_failures"] == 0 and status["items_last_poll"] == 0
    assert out.messages[-1][2]["snapshot"] == {"active_ids": []}


@pytest.mark.parametrize("body,category", [
    ({"type": "Feature"}, "parse"),
    ({"type": "FeatureCollection", "features": [{"properties": {}}]}, "parse"),
])
def test_malformed_responses_are_reported_not_swallowed(cfg, store, body, category):
    feed, _ = make_feed(cfg, store, {NWS: FakeResponse(body=body)})
    result = feed.poll_once(Collector())
    assert result["outcome"] == "error" and result["category"] == category
    status = store.feed_status()[0]
    assert status["consecutive_failures"] == 1 and status["last_error_category"] == category


def test_invalid_json_is_a_parse_error(cfg):
    feed, _ = make_feed(cfg, routes={NWS: FakeResponse(content=b'{"type": "FeatureCollection", "feat')})
    with pytest.raises(FeedError) as err:
        feed.fetch()
    assert err.value.category == "parse"


def test_http_errors_are_categorised(cfg, store):
    feed, _ = make_feed(cfg, store, {NWS: FakeResponse(status=503, content=b"busy")})
    assert feed.poll_once(Collector())["category"] == "http_503"


def test_polygon_alert_keeps_cap_fields_and_geometry(processor):
    data = fixture("nws_active.json")
    item = by_event(data["features"], "Flash Flood Warning")
    events = map_nws(item, processor.context("nws_alerts"))
    assert len(events) == 1
    e = validate(events[0])
    props = item["properties"]
    assert e["event_id"] == f"nws:{props['id']}"
    assert e["type"] == "flooding" and e["subtype"] == "warning" and e["category"] == "weather"
    assert e["severity"] == 4 and e["severity_basis"] == "cap_severity:severe"
    assert e["geometry"]["type"] == item["geometry"]["type"]
    assert e["location"]["confidence"] == "source_area" and "lat" not in e
    assert parse_time(e["expires_at"]) == parse_time(props["expires"])
    assert e["expires_at"].endswith("+00:00")
    assert e["published_at"] is not None
    assert e["references"] == [r["identifier"] for r in props["references"]]
    assert e["attributes"]["ends"] == props["ends"] and e["attributes"]["onset"] == props["onset"]
    assert e["attributes"]["certainty"] == props["certainty"]
    assert e["source_url"] == props["@id"]
    assert e["description"] == props["description"][:2000]


def test_zone_only_alert_uses_cached_zone_polygons(processor):
    item = by_event(fixture("nws_active.json")["features"], "Wind Advisory", "Update")
    assert item["geometry"] is None
    e = validate(map_nws(item, processor.context("nws_alerts"))[0])
    assert e["location"]["evidence"]["method"] == "forecast_zones"
    assert e["geometry"]["type"] == "MultiPolygon" and len(e["geometry"]["coordinates"]) == 3


def test_zone_alert_without_cached_geometry_stays_unresolved(cfg, geocoder):
    item = by_event(fixture("nws_active.json")["features"], "Wind Advisory", "Alert")
    e = validate(map_nws(item, Context("nws_alerts", cfg, geocoder))[0])
    assert e["location"]["confidence"] == "unresolved" and "geometry" not in e


def test_non_road_products_are_filtered_with_a_reason(processor):
    data = fixture("nws_active.json")
    for name in ("Freeze Warning", "Air Quality Alert"):
        events, skipped = normalize(envelope("nws", by_event(data["features"], name), "nws_alerts"), processor.context)
        assert events == [] and skipped == ["event_not_road_relevant"]


def test_test_messages_are_not_hazards(processor):
    item = by_event(fixture("nws_active.json")["features"], "Flood Advisory")
    item = json.loads(json.dumps(item))
    item["properties"]["status"] = "Test"
    assert normalize(envelope("nws", item, "nws_alerts"), processor.context) == ([], ["not_actual"])


def test_feed_defers_alerts_until_zone_geometry_is_cached(cfg, store):
    data = fixture("nws_active.json")
    alert = by_event(data["features"], "Wind Advisory", "Alert")
    zone_url = alert["properties"]["affectedZones"][0]
    zone_id = zone_url.rsplit("/", 1)[-1]
    body = {"type": "FeatureCollection", "features": [alert]}
    feed, session = make_feed(cfg, store, {NWS: [FakeResponse(body=body), FakeResponse(body=body)],
                                           zone_url: [FakeResponse(status=500), FakeResponse(body=fixture(f"nws_zones/{zone_id}.json"))]})
    out = Collector()
    first = feed.poll_once(out)
    assert first["published"] == 0
    feed._zone_failures.clear()
    second = feed.poll_once(out)
    assert second["published"] == 1
    assert store.zone_geometry(zone_id)["type"] == "Polygon"


def test_expired_alert_is_cleared_with_source_expired(processor, clock, store):
    item = by_event(fixture("nws_active.json")["features"], "Flood Advisory")
    clock.set("2026-10-15T17:00:00+00:00")
    processor.handle(envelope("nws", item, "nws_alerts"))
    clock.set("2026-10-20T00:00:00+00:00")
    processor.expire()
    inc = store.read("SELECT * FROM incidents")[0]
    assert inc["state"] == "cleared" and inc["clear_reason"] == "source_expired"
    assert inc["cleared_at"] == inc["expires_at"]


def test_alert_dropped_from_active_feed_is_withdrawn(processor, store):
    item = by_event(fixture("nws_active.json")["features"], "Flood Advisory")
    processor.handle(envelope("nws", item, "nws_alerts"))
    processor.handle({"v": 2, "feed": "nws_alerts", "kind": "nws", "fetched_at": "2026-10-15T17:00:00+00:00",
                      "snapshot": {"active_ids": []}})
    inc = store.read("SELECT * FROM incidents")[0]
    assert inc["state"] == "cleared" and inc["clear_reason"] == "source_withdrawn"


def test_update_message_joins_the_incident_it_references(processor, store):
    update = by_event(fixture("nws_active.json")["features"], "Flash Flood Warning")
    original = json.loads(json.dumps(update))
    ref = update["properties"]["references"][0]
    original["properties"].update(id=ref["identifier"], messageType="Alert", references=[], sent=ref["sent"],
                                  severity="Moderate")
    processor.handle(envelope("nws", original, "nws_alerts"))
    processor.handle(envelope("nws", update, "nws_alerts"))
    incidents = store.read("SELECT * FROM incidents")
    assert len(incidents) == 1
    assert incidents[0]["state"] == "updated" and incidents[0]["evidence_count"] == 2
    assert incidents[0]["severity"] == 4


def test_zone_returned_as_geometry_collection_is_accepted(cfg, store):
    zone = fixture("nws_zones/SDZ050.json")
    collection = dict(zone, geometry={"type": "GeometryCollection", "geometries": [
        zone["geometry"], fixture("nws_zones/SDZ057.json")["geometry"]]})
    url = "https://api.weather.gov/zones/forecast/TXZ313"
    feed, _ = make_feed(cfg, store, {url: FakeResponse(body=collection)})
    feed._fetch_zone(url)
    assert store.zone_geometry("TXZ313")["type"] == "MultiPolygon"


def test_alert_listed_again_after_a_glitch_is_reopened(processor, store):
    item = by_event(fixture("nws_active.json")["features"], "Flood Advisory")
    processor.handle(envelope("nws", item, "nws_alerts"))
    snap = {"v": 2, "feed": "nws_alerts", "kind": "nws", "fetched_at": "2026-10-15T17:00:00+00:00"}
    processor.handle({**snap, "snapshot": {"active_ids": []}})
    assert store.read("SELECT state FROM incidents")[0]["state"] == "cleared"
    processor.handle({**snap, "fetched_at": "2026-10-15T17:01:00+00:00",
                      "snapshot": {"active_ids": [item["properties"]["id"]]}})
    inc = store.read("SELECT state, clear_reason FROM incidents")[0]
    assert inc == {"state": "updated", "clear_reason": None}
    reasons = [h["reason"] for h in store.read("SELECT reason FROM incident_history ORDER BY rowid")]
    assert reasons == ["first_report", "source_withdrawn", "listed_again"]
