import pytest
from src.hazard_enrichment import extract_fields

@pytest.mark.parametrize("text,lanes,conf", [
    ("2 lanes are blocked on I-80 westbound", 2, 0.9),
    ("All lanes closed on US-101", -1, 1.0),
    ("multiple lanes blocked near CA-17", 2, 0.5),
    ("one lane is closed on Main St", 0, 0.0),
])
def test_lanes(text, lanes, conf):
    fx = extract_fields(text)
    assert fx["lanes_blocked"] == lanes
    assert pytest.approx(fx["lanes_blocked_confidence"], 0.01) == conf
    assert fx["extraction_version"] == "v1_regex"

@pytest.mark.parametrize("text,dir_expected", [
    ("Crash on I-5 northbound", "N"),
    ("Stall on SR-99 south", "S"),
    ("Debris on US-50 Eastbound", "E"),
    ("Closure CA-17 WEST", "W"),
    ("No direction mentioned", None),
])
def test_direction(text, dir_expected):
    fx = extract_fields(text)
    assert fx["direction"] == dir_expected

@pytest.mark.parametrize("text,road_expected", [
    ("Accident on I-80 near Exit 12", "I-80"),
    ("Crash on US 101 at Mile Marker 55", "US 101"),
    ("Stall on CA-17 southbound", "CA-17"),
    ("Police activity on Main St", "Main St"),
    ("Fire on Foobar Avenue", None),
])
def test_road(text, road_expected):
    fx = extract_fields(text)
    assert fx["road"] == road_expected

@pytest.mark.parametrize("text,exit_expected,mm_expected", [
    ("Accident at Exit 12 on I-80", 12, None),
    ("Crash near Mile Marker 55 on US-50", None, 55),
    ("Closure at exit 7 on CA-17", 7, None),
    ("Hazard by mile marker 3 on SR-99", None, 3),
    ("No markers here", None, None),
])
def test_exit_and_mm(text, exit_expected, mm_expected):
    fx = extract_fields(text)
    assert fx["exit"] == exit_expected
    assert fx["mile_marker"] == mm_expected