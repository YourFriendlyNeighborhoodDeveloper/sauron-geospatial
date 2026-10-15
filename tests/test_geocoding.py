import pytest

from src.geocoding import Gazetteer, Geocoder, MemoryCache, Nominatim, norm, split_outlet
from tests.conftest import FakeResponse, FakeSession


def resolve(geocoder, title, outlet=None):
    r = geocoder.resolve(title, outlet)
    return r["confidence"], r["place"], r["region"]


def test_outlet_suffix_is_split_from_title():
    assert split_outlet("Crash closes I-80 - KUTV") == ("Crash closes I-80", "KUTV")
    assert split_outlet("No outlet here") == ("No outlet here", None)


def test_name_normalisation():
    assert norm("Mt. Laurel") == "mount laurel"
    assert norm("St. Amant") == "saint amant"
    assert norm("Philadelphia's") == "philadelphia"


def test_state_written_next_to_place_is_high_confidence(geocoder):
    assert resolve(geocoder, "Crash on Route 38 in Mt. Laurel, New Jersey") == ("geocoded_high", "Mount Laurel township", "NJ")
    assert resolve(geocoder, "Topanga, CA – Two hurt in crash on Topanga Canyon Boulevard") == ("geocoded_high", "Topanga CDP", "CA")
    assert resolve(geocoder, "Child dies in crash in Greenville County, South Carolina") == ("geocoded_high", "Greenville County", "SC")


def test_outlet_market_only_disambiguates_names_in_the_text(geocoder):
    assert resolve(geocoder, "Two-car accident in Canton causes road closure", "WWNY") [0] == "geocoded_low"
    assert resolve(geocoder, "Crash on I-80 near Magna", "KUTV") == ("geocoded_medium", "Magna city", "UT")
    assert resolve(geocoder, "Three-vehicle crash in York County", "fox43.com") == ("geocoded_medium", "York County", "PA")
    assert resolve(geocoder, "Three-vehicle crash in York County", "WSOC TV") == ("geocoded_medium", "York County", "SC")


def test_outlet_location_is_never_used_without_a_place_in_the_text(geocoder):
    r = geocoder.resolve("Crash halts I-83 traffic near Exit 39A", "fox43.com")
    assert r["confidence"] == "unresolved" and r["lat"] is None
    assert r["evidence"]["reason"] == "no_place_in_text"


def test_ambiguous_place_without_constraint_is_not_mapped(geocoder):
    r = geocoder.resolve("Rollover crash in Canton slows traffic", "Unknown Gazette")
    assert r["confidence"] == "geocoded_low" and r["lat"] is None
    assert r["evidence"]["candidates"] > 1


def test_state_named_in_text_beats_adjectival_state(geocoder):
    r = geocoder.resolve("Utah mother injured after head-on crash in Florida", "KSLTV.com")
    assert (r["confidence"], r["precision"], r["region"]) == ("unresolved", "state", "FL")


def test_state_name_inside_another_name_is_not_a_state(geocoder):
    r = geocoder.resolve("Crash on I-70 in Kansas City", "KSHB 41 Kansas City")
    assert r["evidence"]["text_states"] == []


def test_road_names_are_not_places(geocoder):
    r = geocoder.resolve("Three-vehicle crash closes portion of Newburg Road in Rockford", "WIFR")
    assert (r["place"], r["region"]) == ("Rockford city", "IL")


def test_non_us_stories_are_out_of_coverage(geocoder):
    for title, outlet in [("Crash in Dundalk leaves five dead", "Irish Examiner"),
                          ("Two-vehicle crash in Waterford", "RTE.ie"),
                          ("Five killed in car crash in County Louth, Ireland", "The Guardian")]:
        r = geocoder.resolve(title, outlet)
        assert r["confidence"] == "unresolved" and r["evidence"]["reason"] == "outside_coverage"


def test_possessive_and_qualifier_are_stripped(geocoder):
    r = geocoder.resolve("Crash on Roosevelt Boulevard in Philadelphia's Hunting Park neighborhood", "6abc Philadelphia")
    assert r["place"] == "Philadelphia city"
    r = geocoder.resolve("Crash shuts down highway between Salinas and Castroville", "KSBW")
    assert r["place"] == "Salinas city"


def test_title_case_headlines_need_the_whole_phrase_to_match(geocoder):
    r = geocoder.resolve("Young Man Killed In Early Morning Crash On Solomons Island Road", "The BayNet")
    assert r["confidence"] == "unresolved"


def test_missing_gazetteer_degrades_to_unresolved(tmp_path):
    g = Geocoder(Gazetteer(str(tmp_path / "missing.db")))
    r = g.resolve("Crash in Magna, Utah")
    assert r["confidence"] == "unresolved" and r["evidence"]["reason"] == "gazetteer_unavailable"


def test_nominatim_fallback_is_throttled_cached_and_state_checked(monkeypatch, gazetteer_db):
    session = FakeSession({"https://nominatim.example/search": [
        FakeResponse(body=[{"display_name": "Somewhere, Utah", "lat": "40.1", "lon": "-111.9", "address": {"state": "Utah"}}]),
    ]})
    sleeps = []
    monkeypatch.setattr("src.geocoding.time.sleep", lambda s: sleeps.append(s))
    nom = Nominatim("https://nominatim.example", "test-agent", MemoryCache(), min_interval=1.1)
    nom.session = session
    assert nom.city("Somewhere", "UT")["lat"] == 40.1
    assert nom.city("Somewhere", "UT")["lat"] == 40.1
    assert len(session.calls) == 1
    assert session.calls[0]["params"]["state"] == "Utah"


def test_nominatim_result_in_wrong_state_is_rejected(monkeypatch):
    session = FakeSession({"https://nominatim.example/search": [
        FakeResponse(body=[{"display_name": "Elsewhere, Ohio", "lat": "40.1", "lon": "-82.9", "address": {"state": "Ohio"}}]),
    ]})
    nom = Nominatim("https://nominatim.example", "test-agent", MemoryCache(), min_interval=0)
    nom.session = session
    assert nom.city("Elsewhere", "UT") is None


@pytest.mark.parametrize("title", ["", "Crash", "1 dead after crash on I-77"])
def test_no_place_returns_no_coordinates(geocoder, title):
    r = geocoder.resolve(title)
    assert r["lat"] is None and r["lon"] is None
