import base64
import json

import pytest

from src import geo
from src.api import AuthConfigError, check_bind, feed_health
from src.dashboard import create_app, location_summary, map_bounds, map_payload, table_rows
from tests.conftest import fixture
from tests.test_processor import raw_log

DATES = {"start_date": "2026-10-01", "end_date": "2026-10-31"}


@pytest.fixture
def loaded(processor, store, cfg):
    for m in raw_log():
        processor.handle(m)
    app = create_app(store, cfg)
    return app, app.server.test_client()


def call(client, output_substring, values):
    deps = client.get("/_dash-dependencies").get_json()
    dep = next(d for d in deps if output_substring in d["output"] and not d.get("clientside_function"))
    def resolve(items):
        return [{"id": i["id"], "property": i["property"], "value": values.get(f"{i['id']}.{i['property']}")} for i in items]
    outputs = [{"id": o.split(".")[0], "property": o.split(".")[1]} for o in dep["output"].strip(".").split("...")]
    body = {"output": dep["output"], "outputs": outputs if len(outputs) > 1 else outputs[0],
            "inputs": resolve(dep["inputs"]), "state": resolve(dep["state"]),
            "changedPropIds": [f"{dep['inputs'][0]['id']}.{dep['inputs'][0]['property']}"]}
    resp = client.post("/_dash-update-component", json=body)
    assert resp.status_code == 200, resp.data[:500]
    return resp.get_json()["response"]


FILTERS = {"tick.n_intervals": 1, "search.value": None, "date_range.start_date": DATES["start_date"],
           "date_range.end_date": DATES["end_date"], "state.value": "all", "categories.value": None,
           "regions.value": None, "min_severity.value": None, "location.value": "any", "dark_mode.value": True}


def map_call(client, **overrides):
    values = {**FILTERS, "type_filter.value": None, "map_signature.data": None, **overrides}
    return call(client, "map_data.data", values)


def test_incident_callback_returns_compact_rows_and_figures(loaded):
    _, client = loaded
    out = call(client, "incident_table.data", FILTERS)
    rows = out["incident_table"]["data"]
    assert rows and {"id", "time", "status", "sev", "location", "basis", "verification", "title"} <= set(rows[0])
    assert all(len(r["time"]) == 16 for r in rows)
    assert len(out["incident_table"]["tooltip_data"]) == len(rows)
    assert out["by_type"]["figure"]["data"]


def test_active_view_hides_cleared_incidents(loaded):
    _, client = loaded
    rows = call(client, "incident_table.data", {**FILTERS, "state.value": "active"})["incident_table"]["data"]
    assert rows and all(r["status"] != "Cleared" for r in rows)
    cleared = call(client, "incident_table.data", {**FILTERS, "state.value": "cleared"})["incident_table"]["data"]
    assert cleared and all(r["status"] == "Cleared" for r in cleared)


def test_verified_filter_excludes_geocoded_locations(loaded):
    _, client = loaded
    rows = call(client, "incident_table.data", {**FILTERS, "location.value": "verified"})["incident_table"]["data"]
    assert rows and all(r["basis"] in ("Verified point", "Warning area") for r in rows)


def test_region_and_severity_filters(loaded):
    _, client = loaded
    tx = call(client, "incident_table.data", {**FILTERS, "regions.value": ["TX"]})["incident_table"]["data"]
    assert tx and all("TX" in r["location"] for r in tx)
    severe = call(client, "incident_table.data", {**FILTERS, "min_severity.value": 4})["incident_table"]["data"]
    assert severe and all(int(r["sev"][0]) >= 4 for r in severe)


def test_overlap_keeps_incidents_active_in_the_window(store, processor):
    for m in raw_log():
        processor.handle(m)
    since, until = "2026-10-15T18:00:00+00:00", "2026-10-15T18:10:00+00:00"
    started = store.query_incidents(state="all", since=since, until=until)
    overlapping = store.query_incidents(state="all", since=since, until=until, overlap=True)
    assert len(overlapping) > len(started)
    assert all(r["event_time"] <= until for r in overlapping)


def test_map_payload_has_polygons_and_points(loaded):
    _, client = loaded
    payload = map_call(client, **{"state.value": "active"})["map_data"]["data"]
    assert payload["areas"] and payload["points"]
    for area in payload["areas"]:
        assert area["geometry"]["type"] in ("Polygon", "MultiPolygon")
        coords = area["geometry"]["coordinates"]
        lon, lat = coords[0][0] if area["geometry"]["type"] == "Polygon" else coords[0][0][0]
        assert -180 <= lon <= -60 and 15 <= lat <= 72, "GeoJSON must stay [lon, lat]"
    (s, w), (n, e) = payload["bounds"]
    assert s < n and w < e


def test_map_payload_is_not_resent_when_unchanged(loaded):
    _, client = loaded
    first = map_call(client)
    sig = first["map_signature"]["data"]
    second = map_call(client, **{"map_signature.data": sig})
    assert "map_data" not in second and "map_legend" in second


def test_map_never_places_unresolved_incidents(loaded, store):
    _, client = loaded
    payload = map_call(client)["map_data"]["data"]
    mapped = {p["id"] for p in payload["points"]} | {a["id"] for a in payload["areas"]}
    hidden = store.read("SELECT incident_id FROM incidents WHERE location_confidence IN ('unresolved','geocoded_low')")
    assert hidden and not mapped & {r["incident_id"] for r in hidden}


def test_points_distinguish_verified_from_approximate():
    base = {"type": "accident", "severity": 3, "state": "open", "title": "t", "verification": "unverified",
            "location_precision": "city", "place": "Magna", "region": "UT", "event_time": "2026-10-15T15:00:00+00:00",
            "last_updated": "2026-10-15T15:00:00+00:00", "category": "roadway", "sources": "rss", "lat": 40.7, "lon": -112.1}
    rows = [{**base, "incident_id": "a", "location_confidence": "geocoded_medium"},
            {**base, "incident_id": "b", "location_confidence": "source_point"}]
    points = {p["id"]: p for p in map_payload(rows, "k")["points"]}
    assert points["a"]["verified"] is False and points["b"]["verified"] is True


def test_tooltips_escape_headline_html():
    row = {"incident_id": "x", "type": "accident", "severity": 2, "state": "open", "verification": "unverified",
           "location_confidence": "geocoded_medium", "location_precision": "city", "place": "Magna", "region": "UT",
           "event_time": "2026-10-15T15:00:00+00:00", "last_updated": "2026-10-15T15:00:00+00:00",
           "category": "roadway", "sources": "rss", "lat": 40.7, "lon": -112.1,
           "title": 'Crash <img src=x onerror="alert(1)">'}
    tip = map_payload([row], "k")["points"][0]["tooltip"]
    assert "<img" not in tip and "&lt;img" in tip


def test_viewport_ignores_a_lone_alert_far_from_the_mainland():
    boxes = [(30, -100, 31, -99), (40, -90, 41, -89), (13, 144, 14, 145)]
    (s, w), (n, e) = map_bounds(boxes)
    assert (s, w, n, e) == (30, -100, 41, -89)


def test_long_area_lists_are_summarised_in_the_table():
    row = {"place": "; ".join(f"County {i}" for i in range(30)), "region": "MN,ND"}
    assert location_summary(row) == "County 0 +29 more · MN, ND"
    data, tips = table_rows([{"incident_id": "i", "event_time": "2026-10-15T23:00:00+00:00", "state": "open",
                              "severity": 3, "category": "weather", "type": "wind", "title": "Wind Advisory",
                              "location_confidence": "source_area", "verification": "official", "sources": "nws",
                              "last_updated": "2026-10-15T22:00:00+00:00", **row}])
    assert data[0]["time"] == "2026-10-15 23:00" and data[0]["location"] == "County 0 +29 more · MN, ND"
    assert tips[0]["location"]["value"].count(";") == 29


def test_health_and_events_callbacks(loaded):
    _, client = loaded
    out = call(client, "health.children", {"tick.n_intervals": 1, "dark_mode.value": True})
    assert "health" in out
    events = call(client, "event_table.data", FILTERS)["event_table"]["data"]
    assert events and all("incident_id" in e for e in events)


def test_detail_callback_shows_full_area_and_evidence(loaded, store):
    _, client = loaded
    incident = store.query_incidents(state="all", categories=["weather"], location="verified")[0]
    out = call(client, "detail.children", {"selected_incident.data": incident["incident_id"], "dark_mode.value": True})
    text = json.dumps(out)
    assert "Evidence" in text and "History" in text and incident["title"] in text
    assert out["map_detail"]["children"] == out["detail"]["children"]
    rules = out["incident_table"]["style_data_conditional"]
    assert any(incident["incident_id"] in r["if"].get("filter_query", "") for r in rules)


def test_selecting_a_table_cell_selects_the_incident(loaded):
    _, client = loaded
    out = call(client, "selected_incident.data", {"incident_table.active_cell": {"row": 0, "column": 0, "row_id": "inc_x"}})
    assert out["selected_incident"]["data"] == "inc_x"


# ---------- API ----------

def test_incidents_api_filters_and_paginates(loaded):
    _, client = loaded
    first = client.get("/api/v1/incidents?state=all&limit=2").get_json()
    assert len(first["data"]) == 2 and first["next_cursor"]
    second = client.get(f"/api/v1/incidents?state=all&limit=2&cursor={first['next_cursor']}").get_json()
    assert {r["incident_id"] for r in first["data"]}.isdisjoint({r["incident_id"] for r in second["data"]})
    seismic = client.get("/api/v1/incidents?state=all&category=seismic").get_json()["data"]
    assert seismic and all(r["category"] == "seismic" for r in seismic)
    assert "geometry" not in first["data"][0] and "report_hash" not in first["data"][0]


@pytest.mark.parametrize("query", ["state=bogus", "limit=0", "since=yesterday", "bbox=1,2,3", "cursor=nope",
                                   "location=maybe"])
def test_incidents_api_rejects_bad_parameters(loaded, query):
    _, client = loaded
    resp = client.get(f"/api/v1/incidents?{query}")
    assert resp.status_code == 400 and resp.get_json()["error"]["code"] == "bad_request"


def test_incident_detail_has_evidence_and_history(loaded, store):
    _, client = loaded
    incident_id = store.query_incidents(state="all", categories=["roadway"])[0]["incident_id"]
    body = client.get(f"/api/v1/incidents/{incident_id}").get_json()
    assert body["incident"]["incident_id"] == incident_id
    assert body["evidence"] and body["history"][0]["reason"] == "first_report"
    assert client.get("/api/v1/incidents/inc_missing").status_code == 404


def test_alerts_at_uses_polygon_containment(loaded):
    _, client = loaded
    warning = next(f for f in fixture("nws_active.json")["features"] if f["properties"]["event"] == "Flash Flood Warning")
    ring = warning["geometry"]["coordinates"][0]
    lat = sum(p[1] for p in ring[:-1]) / (len(ring) - 1)
    lon = sum(p[0] for p in ring[:-1]) / (len(ring) - 1)
    assert geo.contains(warning["geometry"], lat, lon)
    window = "start=2026-10-15T00:00:00Z&end=2026-10-16T00:00:00Z"
    hit = client.get(f"/api/v1/alerts/at?lat={lat}&lon={lon}&{window}").get_json()
    assert [h["event_id"] for h in hit["data"]] == [f"nws:{warning['properties']['id']}"]
    assert "not evidence" in hit["disclaimer"]
    min_lat, min_lon, max_lat, max_lon = geo.bbox(warning["geometry"])
    outside = client.get(f"/api/v1/alerts/at?lat={max_lat + 0.5}&lon={lon}&{window}").get_json()
    assert outside["data"] == []
    early = client.get(f"/api/v1/alerts/at?lat={lat}&lon={lon}&start=2026-10-01T00:00:00Z&end=2026-10-02T00:00:00Z")
    assert early.get_json()["data"] == []
    assert client.get(f"/api/v1/alerts/at?lat={lat}&lon={lon}").status_code == 400


def test_api_token_and_basic_auth(monkeypatch, store, cfg):
    monkeypatch.setenv("SAURON_API_TOKEN", "s3cret-token")
    monkeypatch.setenv("SAURON_DASHBOARD_PASSWORD", "pw")
    client = create_app(store, cfg).server.test_client()
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/v1/incidents").status_code == 401
    assert client.get("/").status_code == 401
    assert client.get("/api/v1/incidents", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/v1/incidents", headers={"Authorization": "Bearer s3cret-token"}).status_code == 200
    basic = base64.b64encode(b"sauron:pw").decode()
    assert client.get("/", headers={"Authorization": f"Basic {basic}"}).status_code == 200


def test_public_bind_without_credentials_is_refused(monkeypatch):
    for var in ("SAURON_DASHBOARD_PASSWORD", "SAURON_API_TOKEN", "SAURON_ALLOW_UNAUTHENTICATED"):
        monkeypatch.delenv(var, raising=False)
    check_bind("127.0.0.1")
    check_bind("localhost")
    with pytest.raises(AuthConfigError):
        check_bind("0.0.0.0")
    monkeypatch.setenv("SAURON_ALLOW_UNAUTHENTICATED", "1")
    check_bind("0.0.0.0")


def test_feed_health_distinguishes_quiet_from_broken(store):
    store.record_poll("quiet", "nws", 60, "2026-10-15T16:59:00+00:00", True, items=0, published=0)
    store.record_poll("old", "usgs", 60, "2026-10-15T10:00:00+00:00", True, items=5, published=5)
    for _ in range(3):
        store.record_poll("broken", "rss", 60, "2026-10-15T16:59:00+00:00", False, error_category="http_503", error="x")
    from datetime import datetime, timezone
    status = {f["feed"]: f["status"] for f in feed_health(store, datetime(2026, 10, 15, 17, 0, tzinfo=timezone.utc))}
    assert status == {"broken": "failing", "old": "stale", "quiet": "ok"}
