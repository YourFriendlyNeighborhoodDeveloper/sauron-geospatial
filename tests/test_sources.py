import copy

import pytest

from src.hazard_enrichment import classify_news, normalize
from src.ingestion.base import FeedError
from src.ingestion.open_data import OpenDataFeed
from src.ingestion.rss_feed import RSSFeed
from src.ingestion.usgs_quakes import USGSFeed
from src.schema import validate
from tests.conftest import Collector, FakeResponse, FakeSession, envelope, fixture

RSS_XML = fixture("news_rss.xml")

USGS_URL = "https://earthquake.usgs.gov/"
SF_URL = "https://data.sfgov.org/resource/wg3w-h783.json"


# ---------- USGS ----------

def test_usgs_keeps_magnitude_depth_and_metadata(processor):
    quake = fixture("usgs_feed.json")["features"][2]
    events, skipped = normalize(envelope("usgs", quake, "usgs_quakes"), processor.context)
    e = validate(events[0])
    props, coords = quake["properties"], quake["geometry"]["coordinates"]
    assert e["event_id"] == f"usgs:{quake['id']}"
    assert e["category"] == "seismic" and e["type"] == "earthquake"
    assert e["attributes"]["magnitude"] == props["mag"] and e["attributes"]["depth_km"] == coords[2]
    assert e["attributes"]["mag_type"] == props["magType"] and e["source_url"] == props["url"]
    assert (e["lat"], e["lon"]) == (round(coords[1], 6), round(coords[0], 6))
    assert e["severity"] == 5 and e["location"]["place"] == props["place"]


def test_minor_quakes_are_filtered_not_hazards(processor):
    for quake in fixture("usgs_feed.json")["features"][:2]:
        assert normalize(envelope("usgs", quake, "usgs_quakes"), processor.context) == ([], ["below_min_magnitude"])


def test_quakes_never_become_roadway_incidents(processor, store):
    processor.handle(envelope("usgs", fixture("usgs_feed.json")["features"][2], "usgs_quakes"))
    assert store.read("SELECT category, type FROM incidents") == [{"category": "seismic", "type": "earthquake"}]


def test_magnitude_revision_is_a_new_version_of_the_same_event(processor, store):
    quake = fixture("usgs_feed.json")["features"][2]
    revised = copy.deepcopy(quake)
    revised["properties"]["mag"] = 6.5
    revised["properties"]["title"] = "M 6.5 - revised"
    processor.handle(envelope("usgs", quake, "usgs_quakes"))
    processor.handle(envelope("usgs", revised, "usgs_quakes"))
    versions = store.read("SELECT version FROM source_events ORDER BY version")
    assert [v["version"] for v in versions] == [1, 2]
    assert store.read("SELECT COUNT(*) n FROM incidents")[0]["n"] == 1


def test_usgs_feed_parses_feature_collection(cfg):
    feed = USGSFeed("usgs_quakes", cfg["feeds"]["usgs_quakes"],
                    session=FakeSession({USGS_URL: FakeResponse(body=fixture("usgs_feed.json"))}))
    assert len(feed.parse(feed.fetch())) == 3
    with pytest.raises(FeedError):
        feed.parse({"error": "x"})


# ---------- San Francisco open data ----------

def test_sf_query_uses_real_category_and_a_multi_day_window(cfg):
    session = FakeSession({SF_URL: FakeResponse(body=fixture("datasf_traffic_collisions.json"))})
    feed = OpenDataFeed("sf_traffic_collisions", cfg["feeds"]["sf_traffic_collisions"], session=session)
    rows = feed.parse(feed.fetch())
    where = session.calls[0]["params"]["$where"]
    assert "incident_category = 'Traffic Collision'" in where and "Vehicle Incident" not in where
    assert len(rows) == 6


def test_sf_rows_keep_source_coordinates_and_local_time(processor):
    row = fixture("datasf_traffic_collisions.json")[0]
    e = validate(normalize(envelope("open_data", row, "sf_traffic_collisions"), processor.context)[0][0])
    assert e["lat"] == pytest.approx(float(row["latitude"])) and e["lon"] == pytest.approx(float(row["longitude"]))
    assert e["location"]["confidence"] == "source_point"
    local = row["incident_datetime"][:16]
    assert e["event_time"] != local and e["event_time"].endswith("+00:00")
    assert e["road"] == " / ".join(" ".join(w.capitalize() for w in p.split()) for p in row["intersection"].split("\\"))


def test_sf_records_are_historical_not_live(processor, store):
    processor.handle(envelope("open_data", fixture("datasf_traffic_collisions.json")[0], "sf_traffic_collisions"))
    inc = store.read("SELECT state, clear_reason FROM incidents")[0]
    assert inc == {"state": "cleared", "clear_reason": "observation_window_elapsed"}


def test_sf_row_without_coordinates_is_unresolved_not_zero(processor):
    row = dict(fixture("datasf_traffic_collisions.json")[0], latitude=None, longitude=None, point=None)
    e = validate(normalize(envelope("open_data", row, "sf_traffic_collisions"), processor.context)[0][0])
    assert e["location"]["confidence"] == "unresolved" and "lat" not in e


# ---------- News RSS ----------

def rss_items(cfg):
    feed = RSSFeed("news_rss", cfg["feeds"]["news_rss"],
                   session=FakeSession({"https://news.google.com": FakeResponse(content=RSS_XML.encode())}))
    return feed.parse(feed.fetch())


def test_rss_feed_keeps_guid_link_outlet_and_utc_time(cfg):
    items = rss_items(cfg)
    assert len(items) == 6
    first = items[0]
    assert first["guid"] == "guid-a" and first["link"] == "https://news.example/a"
    assert first["source_name"] == "KUTV" and first["source_url"] == "https://kutv.com"
    assert first["published"] == "2026-10-15T15:00:00+00:00"


def test_unreadable_rss_is_a_parse_error(cfg):
    feed = RSSFeed("news_rss", cfg["feeds"]["news_rss"],
                   session=FakeSession({"https://news.google.com": FakeResponse(content=b"<html>not rss")}))
    with pytest.raises(FeedError):
        feed.parse(feed.fetch())


@pytest.mark.parametrize("title,expected", [
    ("Multi-vehicle crash closes I-80 westbound near Magna", "incident"),
    ("Driver identified after fatal crash in Canton", "followup"),
    ("Injury lawyers explain what to do after a car crash", "other"),
    ("City council debates new budget", "other"),
    ("Driver charged after crash that injured three", "followup"),
])
def test_news_classification(title, expected):
    assert classify_news(title) == expected


def test_news_events_are_unverified_media(processor, cfg):
    item = rss_items(cfg)[0]
    events, _ = normalize(envelope("rss", item, "news_rss"), processor.context)
    e = validate(events[0])
    assert e["source_reliability"] == "media" and e["category"] == "roadway"
    assert e["title"] == "Multi-vehicle crash closes I-80 westbound near Magna"
    assert e["attributes"]["outlet"] == "KUTV" and e["road"] == "I-80"
    assert e["location"]["confidence"] == "geocoded_medium" and e["location"]["region"] == "UT"
    assert e["provenance"]["event_time"] == "inferred:publication_time"


def test_followups_ads_and_foreign_stories_do_not_become_located_incidents(processor, cfg):
    items = {i["guid"]: i for i in rss_items(cfg)}
    assert normalize(envelope("rss", items["guid-c"], "news_rss"), processor.context)[1] == ["news_followup"]
    assert normalize(envelope("rss", items["guid-d"], "news_rss"), processor.context)[1] == ["news_other"]
    assert normalize(envelope("rss", items["guid-e"], "news_rss"), processor.context) == ([], ["outside_coverage"])
    ambiguous = normalize(envelope("rss", items["guid-f"], "news_rss"), processor.context)[0][0]
    assert ambiguous["location"]["confidence"] == "geocoded_low" and "lat" not in ambiguous


def test_two_outlets_reporting_one_crash_become_one_corroborated_incident(processor, store, cfg):
    items = rss_items(cfg)
    processor.handle(envelope("rss", items[0], "news_rss"))
    processor.handle(envelope("rss", items[1], "news_rss"))
    incidents = store.read("SELECT * FROM incidents")
    assert len(incidents) == 1
    inc = incidents[0]
    assert inc["verification"] == "corroborated" and inc["evidence_count"] == 2
    assert inc["location_confidence"] == "geocoded_high"
    history = store.read("SELECT reason FROM incident_history ORDER BY rowid")
    assert [h["reason"] for h in history] == ["first_report", "new_evidence:spatiotemporal", "reported_reopened"]
    assert (inc["state"], inc["clear_reason"], inc["cleared_at"]) == ("cleared", "reported_reopened", "2026-10-15T15:40:00+00:00")


def test_rss_feed_poll_publishes_each_item_once(cfg, store):
    session = FakeSession({"https://news.google.com": [FakeResponse(content=RSS_XML.encode()),
                                                       FakeResponse(content=RSS_XML.encode())]})
    feed = RSSFeed("news_rss", cfg["feeds"]["news_rss"], store=store, session=session)
    out = Collector()
    assert feed.poll_once(out)["published"] == 6
    assert feed.poll_once(out)["published"] == 0
    assert out.messages[0][1] == "rss:guid-a"
