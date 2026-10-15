from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from zoneinfo import ZoneInfo

from . import geo
from .geocoding import location_from_result, split_outlet
from .schema import SCHEMA_VERSION, parse_time, utc_iso

LANE_PATTERNS = [
    (r'\b(\d+)\s+(?:of\s+the\s+)?lanes?\s+(?:are|is)\s+(?:blocked|closed)\b', lambda m: int(m.group(1))),
    (r'\ball\s+lanes?\s+(?:blocked|closed)\b', lambda m: -1),
    (r'\bmultiple\s+lanes?\s+(?:blocked|closed)\b', lambda m: 2),
]
DIR_PATTERN  = r'\b(north|south|east|west)(?:bound)?\b'
ROAD_PATTERN = r'\b(I[- ]?\d{1,3}|US[- ]?\d+|SR[- ]?\d+|CA[- ]?\d+|[A-Z]{1,3}-\d+|[A-Za-z]+(?:\sRoad| St| Ave| Blvd| Pkwy| Hwy| Highway))\b'
EXIT_PATTERN = r'\bat\s+exit\s+(\d+)\b'
MM_PATTERN   = r'\b(?:near|by|at)\s+mile\s+marker\s+(\d+)\b'


class Skip(Exception):
    """A valid source item that is outside SAURON's scope."""


def extract_fields(text: str) -> Dict[str, Any]:
    t = (text or "").lower()
    lanes, lanes_conf = 0, 0.0
    for pat, fn in LANE_PATTERNS:
        m = re.search(pat, t)
        if m:
            lanes = fn(m)
            lanes_conf = 0.9 if lanes >= 0 else 1.0
            if pat == LANE_PATTERNS[2][0] and lanes == 2:
                lanes_conf = 0.5
            break
    dirm = re.search(DIR_PATTERN, t)
    roadm = re.search(ROAD_PATTERN, text or "")
    exitm = re.search(EXIT_PATTERN, t)
    mmm   = re.search(MM_PATTERN, t)
    return {
        "lanes_blocked": lanes,
        "lanes_blocked_confidence": lanes_conf,
        "direction": dirm.group(1)[0].upper() if dirm else None,
        "road": roadm.group(0) if roadm else None,
        "exit": int(exitm.group(1)) if exitm else None,
        "mile_marker": int(mmm.group(1)) if mmm else None,
        "extraction_version": "v1_regex"
    }


def _event(source, feed, native_id, reliability, category, htype, severity, title, event_time, location,
           lat=None, lon=None, geometry=None, provenance=None, **extra):
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": f"{source}:{native_id}",
        "source": source,
        "feed": feed,
        "source_event_id": str(native_id),
        "source_reliability": reliability,
        "category": category,
        "type": htype,
        "severity": int(severity),
        "title": title.strip()[:300],
        "event_time": event_time,
        "location": location,
        "attributes": extra.pop("attributes", {}),
        "provenance": provenance or {},
    }
    if lat is not None:
        event["lat"], event["lon"] = round(float(lat), 6), round(float(lon), 6)
        event["h3"] = geo.cell(event["lat"], event["lon"])
    if geometry:
        event["geometry"] = geometry
    for key, value in extra.items():
        if value is not None and value != "":
            event[key] = value
    return event


def _ms_to_iso(ms):
    return utc_iso(datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc))


def _plus(iso, minutes):
    return utc_iso(parse_time(iso) + timedelta(minutes=minutes))


# ---------- National Weather Service ----------

CAP_SEVERITY = {"extreme": 5, "severe": 4, "moderate": 3, "minor": 2, "unknown": 1}


def map_nws(item, ctx):
    props = item.get("properties") or {}
    native = props.get("id") or item.get("id")
    if not native:
        raise ValueError("alert without id")
    name = props.get("event") or ""
    htype = ctx.nws_events.get(name)
    if not htype:
        raise Skip("event_not_road_relevant")
    if (props.get("status") or "").lower() != "actual":
        raise Skip("not_actual")
    message_type = (props.get("messageType") or "").lower()
    if message_type == "cancel":
        raise Skip("cancel_message")

    geometry, method = geo.merge([item["geometry"]]) if item.get("geometry") else None, "alert_polygon"
    if not geometry:
        zones = [z.rstrip("/").rsplit("/", 1)[-1] for z in props.get("affectedZones") or []]
        shapes = [ctx.zone_geometry(z) for z in zones]
        geometry = geo.merge([s for s in shapes if s]) if shapes and all(shapes) else None
        method = "forecast_zones"
    ugc = (props.get("geocode") or {}).get("UGC") or []
    region = ",".join(sorted({code[:2] for code in ugc if len(code) >= 2})) or None
    area = (props.get("areaDesc") or "")[:250]
    if geometry:
        geometry = geo.round_geometry(geometry)
        location = {"confidence": "source_area", "precision": "area", "place": area, "region": region,
                    "evidence": {"method": method, "zones": len(props.get("affectedZones") or [])}}
    else:
        location = {"confidence": "unresolved", "precision": "none", "place": area, "region": region,
                    "evidence": {"method": method, "reason": "zone_geometry_unavailable"}}

    sent = props.get("sent")
    start = props.get("onset") or props.get("effective") or sent
    words = name.split()
    subtype = words[-1].lower() if words and words[-1].lower() in ("warning", "watch", "advisory", "statement") else None
    cap = (props.get("severity") or "unknown").lower()
    return [_event(
        "nws", ctx.feed, native, "official", "weather", htype, CAP_SEVERITY.get(cap, 1),
        props.get("headline") or f"{name} for {area}",
        utc_iso(parse_time(start)), location, geometry=geometry,
        subtype=subtype,
        severity_basis=f"cap_severity:{cap}",
        description=(props.get("description") or "")[:2000],
        published_at=utc_iso(parse_time(sent)) if sent else None,
        expires_at=utc_iso(parse_time(props.get("expires"))) if props.get("expires") else None,
        source_status=message_type or None,
        source_url=props.get("@id") or item.get("id"),
        references=[r.get("identifier") for r in props.get("references") or [] if r.get("identifier")],
        attributes={
            "event": name, "certainty": props.get("certainty"), "urgency": props.get("urgency"),
            "effective": props.get("effective"), "onset": props.get("onset"), "ends": props.get("ends"),
            "sender": props.get("senderName"), "ugc": ugc,
        },
        provenance={"geometry": f"source:{method}", "severity": "source:cap", "type": "inferred:event_name"},
    )]


# ---------- Open-Meteo ----------

EXPECTED_UNITS = {"temperature_2m": "°C", "wind_gusts_10m": "km/h", "visibility": "m",
                  "precipitation": "mm", "snowfall": "cm"}
FREEZING_CODES = {56: 3, 57: 4, 66: 3, 67: 4}
SNOW_CODES = {71: 2, 73: 3, 75: 4, 77: 2, 85: 3, 86: 4}


def classify_weather(cur, th, interval=900):
    """Hazards implied by one Open-Meteo 'current' block. Units: km/h, m, mm, cm."""
    per_hour = 3600.0 / (interval or 900)
    gust = cur.get("wind_gusts_10m")
    vis = cur.get("visibility")
    temp = cur.get("temperature_2m")
    code = cur.get("weather_code")
    precip = (cur.get("precipitation") or 0) * per_hour
    liquid = ((cur.get("rain") or 0) + (cur.get("showers") or 0)) * per_hour
    snow = (cur.get("snowfall") or 0) * per_hour
    out = []
    if gust is not None:
        if gust >= th["wind_gust_severe_kmh"]:
            out.append(("wind", 4, f"gust_kmh:{gust}", f"Wind gusts {gust:.0f} km/h ({gust / 1.609:.0f} mph)"))
        elif gust >= th["wind_gust_kmh"]:
            out.append(("wind", 3, f"gust_kmh:{gust}", f"Wind gusts {gust:.0f} km/h ({gust / 1.609:.0f} mph)"))
    if vis is not None:
        if vis <= th["dense_fog_visibility_m"]:
            out.append(("fog", 3, f"visibility_m:{vis}", f"Visibility {vis:.0f} m"))
        elif code in (45, 48) and vis <= th["fog_visibility_m"]:
            out.append(("fog", 2, f"visibility_m:{vis}", f"Fog, visibility {vis:.0f} m"))
    if code in FREEZING_CODES:
        out.append(("ice", FREEZING_CODES[code], f"wmo_code:{code}", "Freezing drizzle or rain"))
    elif temp is not None and temp <= 0 and liquid > 0:
        out.append(("ice", 3, f"temp_c:{temp},liquid_mmh:{liquid:.1f}", f"Liquid precipitation at {temp:.1f} °C"))
    if code in SNOW_CODES or snow > 0:
        sev = SNOW_CODES.get(code, 2)
        if snow >= th["heavy_snow_cmh"]:
            sev = max(sev, 4)
        out.append(("snow", sev, f"snowfall_cmh:{snow:.1f},wmo_code:{code}", f"Snowfall {snow:.1f} cm/h"))
    if precip >= th["heavy_rain_mmh"] and not snow:
        sev = 4 if precip >= th["extreme_rain_mmh"] else 3
        out.append(("heavy_rain", sev, f"precip_mmh:{precip:.1f}", f"Rain {precip:.1f} mm/h"))
    if code in (95, 96, 99):
        out.append(("thunderstorm", 3, f"wmo_code:{code}", "Thunderstorm"))
    return out


def map_open_meteo(item, ctx):
    point = item.get("point") or {}
    cur = item.get("current") or {}
    units = item.get("units") or {}
    for key, unit in EXPECTED_UNITS.items():
        if key in cur and units.get(key) != unit:
            raise ValueError(f"unexpected unit for {key}: {units.get(key)!r}")
    if not cur.get("time"):
        raise ValueError("observation without time")
    obs = datetime.fromisoformat(cur["time"]).replace(tzinfo=timezone.utc)
    obs_iso = utc_iso(obs)
    key = point.get("name") or f"{point.get('lat')},{point.get('lon')}"
    slug = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")
    conditions = classify_weather(cur, ctx.weather_thresholds, cur.get("interval") or 900)
    if not conditions:
        raise Skip("no_hazard_conditions")
    location = {"confidence": "source_point", "precision": "point", "place": point.get("name"), "region": None,
                "evidence": {"method": "configured_point"}}
    events = []
    for htype, severity, basis, text in conditions:
        events.append(_event(
            "open_meteo", ctx.feed, f"{slug}:{htype}:{obs_iso}", "model", "weather", htype, severity,
            f"{text} near {point.get('name') or key}", obs_iso, location,
            lat=point.get("lat"), lon=point.get("lon"),
            severity_basis=basis,
            description=f"{text}. Model conditions for the configured point, not a station observation.",
            published_at=obs_iso,
            expires_at=_plus(obs_iso, ctx.ttl("open_meteo")),
            series_key=f"open_meteo:{slug}:{htype}",
            attributes={"current": cur, "units": units, "grid": item.get("grid")},
            provenance={"type": "computed:thresholds", "severity": "computed:thresholds"},
        ))
    return events


# ---------- USGS ----------

def _quake_severity(mag):
    for limit, sev in ((6.0, 5), (5.0, 4), (4.0, 3), (3.0, 2)):
        if mag >= limit:
            return sev
    return 1


def map_usgs(item, ctx):
    props = item.get("properties") or {}
    native = item.get("id")
    coords = (item.get("geometry") or {}).get("coordinates") or []
    if not native or len(coords) < 2 or props.get("time") is None:
        raise ValueError("feature missing id, coordinates or time")
    if props.get("type") != "earthquake":
        raise Skip("not_earthquake")
    mag = props.get("mag")
    if mag is None or mag < ctx.min_magnitude:
        raise Skip("below_min_magnitude")
    lon, lat = float(coords[0]), float(coords[1])
    depth = float(coords[2]) if len(coords) > 2 and coords[2] is not None else None
    event_time = _ms_to_iso(props["time"])
    location = {"confidence": "source_point", "precision": "point", "place": props.get("place"),
                "region": None, "evidence": {"method": "epicenter"}}
    return [_event(
        "usgs", ctx.feed, native, "official", "seismic", "earthquake", _quake_severity(mag),
        props.get("title") or f"M {mag}", event_time, location, lat=lat, lon=lon,
        subtype=props.get("magType"),
        severity_basis=f"magnitude:{mag}",
        published_at=_ms_to_iso(props["updated"]) if props.get("updated") else None,
        expires_at=_plus(event_time, ctx.ttl("usgs")),
        source_status=props.get("status"),
        source_url=props.get("url"),
        attributes={"magnitude": mag, "mag_type": props.get("magType"), "depth_km": depth,
                    "tsunami": props.get("tsunami"), "felt": props.get("felt"), "mmi": props.get("mmi"),
                    "alert": props.get("alert"), "significance": props.get("sig"), "network": props.get("net"),
                    "ids": [i for i in (props.get("ids") or "").split(",") if i and i != native]},
        provenance={"severity": "computed:magnitude_bands"},
    )]


# ---------- San Francisco open data (SFPD incident reports) ----------

def map_open_data(row, ctx):
    native = row.get("row_id")
    when = row.get("incident_datetime")
    if not native or not when:
        raise ValueError("row missing row_id or incident_datetime")
    if (row.get("incident_category") or "") != "Traffic Collision":
        raise Skip("not_traffic_collision")
    tz = ZoneInfo(ctx.local_timezone)
    event_time = utc_iso(datetime.fromisoformat(when).replace(tzinfo=tz))
    reported = row.get("report_datetime")
    lat = _float(row.get("latitude"))
    lon = _float(row.get("longitude"))
    road = _intersection(row.get("intersection"))
    if lat is not None and lon is not None and not (lat == 0 and lon == 0):
        location = {"confidence": "source_point", "precision": "point", "place": "San Francisco", "region": "CA",
                    "evidence": {"method": "source_coordinates"}}
    else:
        lat = lon = None
        location = {"confidence": "unresolved", "precision": "none", "place": "San Francisco", "region": "CA",
                    "evidence": {"reason": "no_coordinates_in_record"}}
    description = row.get("incident_description") or row.get("incident_subcategory") or "Traffic collision"
    return [_event(
        "open_data", ctx.feed, native, "official", "roadway", "accident", 2,
        f"{description} at {road}" if road else description, event_time, location, lat=lat, lon=lon,
        subtype=(row.get("incident_subcategory") or "").lower() or None,
        severity_basis="default:police_report",
        published_at=utc_iso(datetime.fromisoformat(reported).replace(tzinfo=tz)) if reported else None,
        expires_at=_plus(event_time, ctx.ttl("open_data")),
        road=road,
        source_status=(row.get("report_type_description") or "").lower() or None,
        source_url="https://data.sfgov.org/d/wg3w-h783",
        attributes={"incident_id": row.get("incident_id"), "resolution": row.get("resolution"),
                    "police_district": row.get("police_district"), "neighborhood": row.get("analysis_neighborhood"),
                    "data_loaded_at": row.get("data_loaded_at")},
        provenance={"event_time": f"source:local_time:{ctx.local_timezone}"},
    )]


def _intersection(value):
    if not value:
        return None
    parts = [" ".join(w.capitalize() for w in p.split()) for p in value.split("\\") if p.strip()]
    return " / ".join(parts)[:128] or None


def _float(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ---------- News RSS ----------

CRASH_WORDS = re.compile(r"\b(crash|crashes|collision|wreck|pile-?up|rollover|overturn(?:s|ed)?|"
                         r"accident|struck|hit-and-run|jackknifed)\b", re.I)
CLOSURE_WORDS = re.compile(r"\b(closed|closes|closure|closures|shut down|shuts down|blocked|blocks|"
                           r"lanes? (?:closed|blocked)|reopens?|reopened)\b", re.I)
FOLLOWUP_WORDS = re.compile(
    r"\b(identif(?:y|ied|ies)|ID['’]d|named|name of|release(?:s|d)? names?|charged|charges|sentenced|"
    r"pleads?|lawsuit|sues|mourn(?:s|ing)?|tributes?|vigil|funeral|memorial|remember(?:ed|ing)?|"
    r"later dies|more details|details released|believed a factor|cause of)\b", re.I)
REOPENED_WORDS = re.compile(r"\b(reopen(?:s|ed)?|re-opened|lanes? (?:are |now )?(?:open|cleared)|all clear)\b", re.I)
OTHER_WORDS = re.compile(r"\b(lawyers?|attorneys?|law firm|injury law|steps for|tips|donate|statistics|"
                         r"so far this year|over (?:six|seven|\d+) days|bill|legislation)\b", re.I)


def classify_news(title):
    if not CRASH_WORDS.search(title) and not CLOSURE_WORDS.search(title):
        return "other"
    if OTHER_WORDS.search(title):
        return "other"
    if FOLLOWUP_WORDS.search(title):
        return "followup"
    return "incident"


def map_rss(item, ctx):
    raw_title = (item.get("title") or "").strip()
    native = item.get("guid") or item.get("link")
    if not raw_title or not native:
        raise ValueError("entry missing title or guid")
    outlet = item.get("source_name")
    title, suffix = split_outlet(raw_title)
    if outlet and suffix and suffix != outlet:
        title = raw_title
    outlet = outlet or suffix
    kind = classify_news(title)
    if kind != "incident":
        raise Skip(f"news_{kind}")
    published = item.get("published")
    if not published:
        raise ValueError("entry without publication time")
    published = utc_iso(parse_time(published))
    result = ctx.geocoder.resolve(title, outlet, item.get("source_url"))
    if result["evidence"].get("reason") == "outside_coverage":
        raise Skip("outside_coverage")
    fields = extract_fields(title)
    closure = bool(CLOSURE_WORDS.search(title))
    severity = 2
    if re.search(r"\b(all lanes|shut down|shuts down|closes|closed)\b", title, re.I):
        severity = 4
    elif re.search(r"\b(blocked|blocks|lanes|delays|backups?|backed up)\b", title, re.I):
        severity = 3
    lanes = fields["lanes_blocked"]
    return [_event(
        "rss", ctx.feed, hashlib.sha1(native.encode()).hexdigest()[:20], "media", "roadway",
        "accident" if CRASH_WORDS.search(title) else "closure", severity, title, published,
        location_from_result(result), lat=result["lat"], lon=result["lon"],
        subtype="road_closure" if closure else None,
        severity_basis="inferred:headline_keywords",
        published_at=published,
        expires_at=_plus(published, ctx.ttl("rss")),
        road=fields["road"], direction=fields["direction"],
        source_url=item.get("link"),
        source_status="reopened" if REOPENED_WORDS.search(title) else None,
        attributes={"outlet": outlet, "outlet_url": item.get("source_url"), "guid": native,
                    "lanes_blocked": lanes or None, "exit": fields["exit"], "mile_marker": fields["mile_marker"]},
        provenance={"event_time": "inferred:publication_time", "location": f"inferred:{result['evidence'].get('method')}",
                    "road": "inferred:v1_regex", "direction": "inferred:v1_regex", "severity": "inferred:headline_keywords"},
    )]


MAPPERS = {"nws": map_nws, "open_meteo": map_open_meteo, "usgs": map_usgs, "open_data": map_open_data, "rss": map_rss}


class Context:
    def __init__(self, feed, cfg, geocoder=None, zone_geometry=None):
        self.feed = feed
        self.cfg = cfg
        self.geocoder = geocoder
        self.zone_geometry = zone_geometry or (lambda zone: None)
        feed_cfg = (cfg.get("feeds") or {}).get(feed) or {}
        self.nws_events = feed_cfg.get("events") or {}
        self.weather_thresholds = {**DEFAULT_THRESHOLDS, **(feed_cfg.get("thresholds") or {})}
        self.min_magnitude = float(feed_cfg.get("min_magnitude", 2.5))
        self.local_timezone = feed_cfg.get("timezone", "America/Los_Angeles")

    def ttl(self, source):
        return int(((self.cfg.get("lifecycle") or {}).get("ttl_minutes") or {}).get(source, DEFAULT_TTL[source]))


DEFAULT_TTL = {"rss": 360, "open_data": 360, "usgs": 1440, "open_meteo": 45, "nws": 0}
DEFAULT_THRESHOLDS = {
    "wind_gust_kmh": 72, "wind_gust_severe_kmh": 93,
    "dense_fog_visibility_m": 400, "fog_visibility_m": 1000,
    "heavy_snow_cmh": 2.5, "heavy_rain_mmh": 7.6, "extreme_rain_mmh": 25,
}


def normalize(envelope, ctx_for):
    """Map one raw envelope to (events, skipped_reasons). Raises ValueError on malformed input."""
    kind = envelope.get("kind") or envelope.get("source")
    mapper = MAPPERS.get(kind)
    if mapper is None:
        raise ValueError(f"no mapper for kind {kind!r}")
    feed = envelope.get("feed") or envelope.get("source") or kind
    try:
        return mapper(envelope.get("item") or {}, ctx_for(feed)), []
    except Skip as skip:
        return [], [str(skip)]
