import copy

import pytest

from src.hazard_enrichment import DEFAULT_THRESHOLDS, classify_weather, normalize
from src.ingestion.base import FeedError
from src.ingestion.open_meteo import OpenMeteoFeed
from src.schema import validate
from tests.conftest import Collector, FakeResponse, FakeSession, envelope, fixture

URL = "https://api.open-meteo.com/v1/forecast"


def observation(**current):
    raw = fixture("open_meteo_calm.json")
    item = {"point": {"name": "SF Downtown", "lat": 37.7749, "lon": -122.4194},
            "current": {**raw["current"], **current}, "units": dict(raw["current_units"]),
            "grid": {"lat": raw["latitude"], "lon": raw["longitude"], "elevation": raw["elevation"]}}
    return item


def kinds(item, processor):
    events, skipped = normalize(envelope("open_meteo", item), processor.context)
    return [(e["type"], e["severity"]) for e in events], skipped


def test_live_calm_response_has_the_units_the_rules_assume():
    units = fixture("open_meteo_calm.json")["current_units"]
    assert units["wind_gusts_10m"] == "km/h"
    assert units["visibility"] == "m"
    assert units["snowfall"] == "cm"


def test_calm_conditions_produce_no_hazard(processor):
    assert kinds(observation(), processor) == ([], ["no_hazard_conditions"])


@pytest.mark.parametrize("gust", [18.5, 20.0, 40.0, 71.9])
def test_ordinary_gusts_are_not_wind_hazards(processor, gust):
    """Regression: the old code compared km/h values to an m/s threshold of 18 (~11 mph)."""
    assert kinds(observation(wind_gusts_10m=gust), processor)[0] == []


def test_advisory_and_warning_level_gusts(processor):
    assert kinds(observation(wind_gusts_10m=80.0), processor)[0] == [("wind", 3)]
    assert kinds(observation(wind_gusts_10m=100.0), processor)[0] == [("wind", 4)]


def test_visibility_is_read_in_metres(processor):
    """Regression: the old code treated metres as km, so fog could only fire below 1 m."""
    assert kinds(observation(visibility=21200.0), processor)[0] == []
    assert kinds(observation(visibility=350.0), processor)[0] == [("fog", 3)]
    assert kinds(observation(visibility=800.0, weather_code=45), processor)[0] == [("fog", 2)]
    assert kinds(observation(visibility=800.0, weather_code=0), processor)[0] == []


def test_freezing_temperature_alone_is_not_snow(processor):
    """Regression: the old code labelled any dry reading at or below 0 C as snow."""
    assert kinds(observation(temperature_2m=-4.0), processor)[0] == []


def test_snow_and_ice_need_precipitation_or_a_weather_code(processor):
    assert kinds(observation(temperature_2m=-2.0, snowfall=0.8, weather_code=75), processor)[0] == [("snow", 4)]
    assert kinds(observation(temperature_2m=-1.0, weather_code=71, snowfall=0.1), processor)[0] == [("snow", 2)]
    assert kinds(observation(temperature_2m=-1.2, rain=0.4, weather_code=67), processor)[0] == [("ice", 4)]
    assert kinds(observation(temperature_2m=-0.5, rain=0.3, weather_code=61), processor)[0] == [("ice", 3)]


def test_precipitation_is_converted_to_an_hourly_rate():
    th = DEFAULT_THRESHOLDS
    assert classify_weather({"precipitation": 2.0}, th, interval=900) == [("heavy_rain", 3, "precip_mmh:8.0", "Rain 8.0 mm/h")]
    assert classify_weather({"precipitation": 2.0}, th, interval=3600) == []


def test_unexpected_units_are_rejected(processor):
    item = observation(wind_gusts_10m=20.0)
    item["units"]["wind_gusts_10m"] = "m/s"
    with pytest.raises(ValueError):
        normalize(envelope("open_meteo", item), processor.context)


def test_thresholds_come_from_config(processor, cfg):
    cfg["feeds"]["open_meteo"]["thresholds"]["wind_gust_kmh"] = 30
    processor._contexts.clear()
    assert kinds(observation(wind_gusts_10m=35.0), processor)[0] == [("wind", 3)]


def test_event_is_schema_valid_and_point_located(processor):
    events, _ = normalize(envelope("open_meteo", observation(wind_gusts_10m=85.0)), processor.context)
    e = validate(events[0])
    assert e["source_reliability"] == "model" and e["location"]["confidence"] == "source_point"
    assert e["series_key"] == "open_meteo:sf-downtown:wind"
    assert e["expires_at"] > e["event_time"]


def test_unchanged_condition_is_not_stored_every_poll(processor, store):
    first = observation(wind_gusts_10m=85.0, time="2026-10-15T16:30")
    second = observation(wind_gusts_10m=88.0, time="2026-10-15T16:45")
    third = observation(wind_gusts_10m=99.0, time="2026-10-15T17:00")
    processor.handle(envelope("open_meteo", first))
    processor.handle(envelope("open_meteo", second))
    assert store.read("SELECT COUNT(*) n FROM source_events")[0]["n"] == 1
    inc = store.read("SELECT * FROM incidents")[0]
    assert inc["expires_at"] == "2026-10-15T17:30:00+00:00"
    processor.handle(envelope("open_meteo", third))
    assert store.read("SELECT COUNT(*) n FROM source_events")[0]["n"] == 2
    assert store.read("SELECT severity, state FROM incidents")[0] == {"severity": 4, "state": "updated"}


def test_condition_not_reobserved_clears_incident(processor, store, clock):
    processor.handle(envelope("open_meteo", observation(wind_gusts_10m=85.0, time="2026-10-15T16:30")))
    clock.set("2026-10-15T18:00:00+00:00")
    processor.expire()
    inc = store.read("SELECT * FROM incidents")[0]
    assert inc["state"] == "cleared" and inc["clear_reason"] == "not_reobserved"


def test_feed_requests_explicit_units(cfg):
    raw = fixture("open_meteo_calm.json")
    session = FakeSession({URL: FakeResponse(body=raw)})
    feed_cfg = copy.deepcopy(cfg["feeds"]["open_meteo"])
    feed_cfg["points"] = feed_cfg["points"][:1]
    feed = OpenMeteoFeed("open_meteo", feed_cfg, session=session)
    items = feed.parse(feed.fetch())
    params = session.calls[0]["params"]
    assert params["wind_speed_unit"] == "kmh" and params["precipitation_unit"] == "mm"
    assert "weather_code" in params["current"] and "apikey" not in params
    assert items[0]["units"] == raw["current_units"]


def test_commercial_key_switches_endpoint(cfg, monkeypatch):
    monkeypatch.setenv("OPEN_METEO_API_KEY", "test-key")
    feed = OpenMeteoFeed("open_meteo", cfg["feeds"]["open_meteo"], session=FakeSession())
    assert feed.url.startswith("https://customer-api.open-meteo.com/")


def test_all_points_failing_is_a_feed_error(cfg, store):
    feed_cfg = copy.deepcopy(cfg["feeds"]["open_meteo"])
    feed_cfg["points"] = feed_cfg["points"][:2]
    feed = OpenMeteoFeed("open_meteo", feed_cfg, store=store, session=FakeSession({URL: FakeResponse(status=429)}))
    result = feed.poll_once(Collector())
    assert result["outcome"] == "error" and result["category"] == "all_points_failed"


def test_missing_current_block_is_a_parse_error(cfg):
    feed = OpenMeteoFeed("open_meteo", cfg["feeds"]["open_meteo"], session=FakeSession())
    with pytest.raises(FeedError):
        feed.parse([{"point": {"name": "x"}, "data": {"error": True}}])
