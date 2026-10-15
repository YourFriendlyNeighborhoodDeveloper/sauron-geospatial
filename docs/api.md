# API

Read-only JSON over HTTP, served by the dashboard process (default `http://127.0.0.1:8050`).

## Authentication

Off by default (the server only listens on loopback). When `SAURON_API_TOKEN` is set, send
`Authorization: Bearer <token>`. When `SAURON_DASHBOARD_PASSWORD` is set, HTTP Basic with
`SAURON_DASHBOARD_USER` / password also works. Failures return `401` with an error body. `/healthz` never needs
credentials.

## Errors

```json
{"error": {"code": "bad_request", "message": "state must be active, cleared or all"}}
```

`400 bad_request`, `401 unauthorized`, `404 not_found`, `503 store_unavailable`.

## `GET /healthz`

`{"status": "ok"}` while the process is serving.

## `GET /readyz`

Store counts and per-feed health. `status` is `degraded` when any feed is `failing`, `stale` or
`never_succeeded`.

## `GET /api/v1/incidents`

| Parameter | Default | Meaning |
|---|---|---|
| `state` | `active` | `active` (open or updated), `cleared`, `all` |
| `category` | all | Comma list: `roadway,weather,seismic` |
| `type` | all | Comma list, e.g. `flooding,wind` |
| `since`, `until` | none | ISO-8601 with timezone, compared with `event_time` |
| `bbox` | none | `minLon,minLat,maxLon,maxLat`; matches point incidents and polygons intersecting the box |
| `location` | `any` | `mappable` (has coordinates or polygon) or `verified` (source-provided only) |
| `q` | none | Case-insensitive text match on title, place, road, source, type |
| `limit` | 100 | 1-500 |
| `cursor` | none | `next_cursor` from the previous page |

Results are ordered by `event_time` descending, then `incident_id`. Pagination is keyset-based, so pages stay
stable while new incidents arrive.

Response from the offline demo (`python -m src --demo`), description shortened:

```json
{
  "data": [
    {
      "category": "weather",
      "clear_reason": null,
      "cleared_at": null,
      "description": "* WHAT...Flooding caused by excessive rainfall is expected.\n...",
      "direction": null,
      "event_time": "2026-10-15T16:52:00+00:00",
      "evidence_count": 1,
      "expires_at": "2026-10-15T19:45:00+00:00",
      "first_observed": "2026-10-15T16:59:00+00:00",
      "h3": null,
      "incident_id": "inc_38ce0a8ab3c8014c",
      "last_updated": "2026-10-15T16:52:00+00:00",
      "lat": null,
      "location_confidence": "source_area",
      "location_precision": "area",
      "lon": null,
      "max_lat": 33.84,
      "max_lon": -99.75,
      "min_lat": 33.4,
      "min_lon": -100.0,
      "place": "Foard, TX; Knox, TX",
      "region": "TX",
      "road": null,
      "schema_version": 2,
      "series_key": null,
      "severity": 2,
      "sources": "nws",
      "state": "open",
      "subtype": "advisory",
      "title": "Flood Advisory issued October 15 at 11:52AM CDT until October 15 at 2:45PM CDT by NWS Norman OK",
      "type": "flooding",
      "verification": "official"
    }
  ],
  "next_cursor": "WyIyMDI2LTEwLTE1VDE2OjUyOjAwKzAwOjAwIiwgImluY18zOGNlMGE4YWIzYzgwMTRjIl0="
}
```

Geometry is omitted from list results; fetch a single incident for it.

## `GET /api/v1/incidents/{incident_id}`

The incident (with `geometry` as GeoJSON and `clear_reason_text`), every piece of evidence (latest version of
each source event, with its location basis and provenance), and its state history.

## `GET /api/v1/alerts/at?lat=&lon=&start=&end=`

Every version of every stored NWS alert whose polygon contains the point and whose validity
(`event_time`..`expires_at`) overlaps `[start, end]`. `start` and `end` are required ISO-8601 timestamps. Candidates
come from a bounding-box index; containment is exact point-in-polygon on the stored geometry (zone polygons are
simplified to about 50 m, so points within that distance of a zone edge can be misclassified).

Response from the offline demo for a point inside the Foard/Knox County flood advisory polygon:

```json
{
  "data": [
    {
      "area": "Foard, TX; Knox, TX",
      "attributes": {
        "certainty": "Likely",
        "ends": "2026-10-15T14:45:00-05:00",
        "event": "Flood Advisory",
        "onset": "2026-10-15T11:52:00-05:00",
        "urgency": "Expected"
      },
      "event_id": "nws:urn:oid:2.49.0.1.840.0.53eb0b79cd3e3fda2b63ba1dc27f575850469736.001.1",
      "event_time": "2026-10-15T16:52:00+00:00",
      "expires_at": "2026-10-15T19:45:00+00:00",
      "incident_id": "inc_38ce0a8ab3c8014c",
      "published_at": "2026-10-15T16:52:00+00:00",
      "severity": 2,
      "source": "nws",
      "source_url": "https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0.53eb0b79cd3e3fda2b63ba1dc27f575850469736.001.1",
      "title": "Flood Advisory issued October 15 at 11:52AM CDT until October 15 at 2:45PM CDT by NWS Norman OK",
      "type": "flooding",
      "version": 1
    }
  ],
  "disclaimer": "NWS warnings describe forecast or observed hazard areas. A warning covering a location is not evidence that the location was damaged.",
  "query": {
    "end": "2026-10-16T00:00:00+00:00",
    "lat": 33.636,
    "lon": -99.864,
    "start": "2026-10-15T00:00:00+00:00"
  }
}
```

Only alerts SAURON stored while running are searchable; there is no historical backfill.
