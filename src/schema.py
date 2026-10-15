import hashlib
import json
from datetime import datetime, timezone

SCHEMA_VERSION = 2

CATEGORIES = {"roadway", "weather", "seismic"}

# How the event's location was established, strongest first.
LOCATION_CONFIDENCE = (
    "source_point",      # coordinates published by the source
    "source_area",       # polygon published by the source (or its forecast zones)
    "geocoded_high",     # place named in the text, state named in the text
    "geocoded_medium",   # place named in the text, state from outlet market or a unique name
    "geocoded_low",      # place named in the text but ambiguous; not mapped
    "unresolved",
)
VERIFIED_LOCATIONS = {"source_point", "source_area"}
MAPPABLE_LOCATIONS = {"source_point", "source_area", "geocoded_high", "geocoded_medium"}

LOCATION_PRECISION = {"point", "area", "city", "county", "state", "none"}

SOURCE_RELIABILITY = {
    "official": "Government publisher (NWS, USGS, police records)",
    "model": "Weather model analysis, not a direct observation",
    "media": "News report; unverified unless corroborated",
}

INCIDENT_STATES = ("open", "updated", "cleared")

CLEAR_REASONS = {
    "source_expired": "Source message expired; the hazard itself may persist",
    "source_withdrawn": "No longer listed by the source before its expiry (cancelled or replaced)",
    "not_reobserved": "Condition absent from later observations",
    "observation_window_elapsed": "No new reports within the observation window",
    "reported_reopened": "A news report says the road reopened (unverified)",
}

REQUIRED_FIELDS = {
    "schema_version": int,
    "event_id": str,
    "source": str,
    "feed": str,
    "source_event_id": str,
    "source_reliability": str,
    "category": str,
    "type": str,
    "severity": int,
    "title": str,
    "event_time": str,
    "location": dict,
    "attributes": dict,
    "provenance": dict,
}

OPTIONAL_FIELDS = {
    "source_url": str,
    "subtype": str,
    "severity_basis": str,
    "description": str,
    "published_at": str,
    "expires_at": str,
    "source_status": str,
    "references": list,
    "series_key": str,
    "lat": float,
    "lon": float,
    "geometry": dict,
    "h3": str,
    "road": str,
    "direction": str,
}

TIME_FIELDS = ("event_time", "published_at", "expires_at")

# Fields that make a new version of a source event. Observation timestamps
# and expiry are excluded so that re-polling an unchanged record is a no-op.
CONTENT_FIELDS = (
    "category", "type", "subtype", "severity", "title", "description",
    "lat", "lon", "geometry", "road", "direction", "source_status", "references",
)


class SchemaError(ValueError):
    pass


def utc_iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise SchemaError("naive datetime")
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_time(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise SchemaError(f"timestamp without timezone: {value!r}")
    return dt.astimezone(timezone.utc)


def content_hash(event):
    material = {k: event.get(k) for k in CONTENT_FIELDS}
    material["location"] = {k: event["location"].get(k) for k in ("confidence", "precision", "place", "region")}
    blob = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def validate(event):
    for name, kind in REQUIRED_FIELDS.items():
        if name not in event or event[name] is None:
            raise SchemaError(f"missing field {name}")
        if not isinstance(event[name], kind) or (kind is int and isinstance(event[name], bool)):
            raise SchemaError(f"{name} must be {kind.__name__}")
    for name, kind in OPTIONAL_FIELDS.items():
        value = event.get(name)
        if value is None:
            continue
        if kind is float and isinstance(value, int) and not isinstance(value, bool):
            continue
        if not isinstance(value, kind):
            raise SchemaError(f"{name} must be {kind.__name__}")
    unknown = set(event) - set(REQUIRED_FIELDS) - set(OPTIONAL_FIELDS)
    if unknown:
        raise SchemaError(f"unknown fields {sorted(unknown)}")

    if event["schema_version"] != SCHEMA_VERSION:
        raise SchemaError(f"schema_version {event['schema_version']} != {SCHEMA_VERSION}")
    if not event["event_id"].startswith(event["source"] + ":"):
        raise SchemaError("event_id must be prefixed with the source")
    if event["category"] not in CATEGORIES:
        raise SchemaError(f"unknown category {event['category']!r}")
    if event["source_reliability"] not in SOURCE_RELIABILITY:
        raise SchemaError(f"unknown source_reliability {event['source_reliability']!r}")
    if not 1 <= event["severity"] <= 5:
        raise SchemaError("severity must be 1-5")
    if not event["title"].strip():
        raise SchemaError("empty title")

    for name in TIME_FIELDS:
        if event.get(name) is not None:
            parse_time(event[name])

    loc = event["location"]
    confidence = loc.get("confidence")
    if confidence not in LOCATION_CONFIDENCE:
        raise SchemaError(f"unknown location confidence {confidence!r}")
    if loc.get("precision") not in LOCATION_PRECISION:
        raise SchemaError(f"unknown location precision {loc.get('precision')!r}")
    lat, lon = event.get("lat"), event.get("lon")
    if (lat is None) != (lon is None):
        raise SchemaError("lat and lon must be set together")
    if lat is not None:
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise SchemaError("coordinates out of range")
        if lat == 0 and lon == 0:
            raise SchemaError("0,0 is not a location")
        if confidence not in MAPPABLE_LOCATIONS or confidence == "source_area":
            raise SchemaError(f"point coordinates not allowed with {confidence}")
    if confidence in ("source_point", "geocoded_high", "geocoded_medium") and lat is None:
        raise SchemaError(f"{confidence} requires coordinates")
    if confidence == "source_area" and not event.get("geometry"):
        raise SchemaError("source_area requires geometry")
    if confidence in ("geocoded_low", "unresolved") and (lat is not None or event.get("geometry")):
        raise SchemaError(f"{confidence} must not carry coordinates")
    return event
