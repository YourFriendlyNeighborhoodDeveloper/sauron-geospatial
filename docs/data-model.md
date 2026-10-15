# Data model

## Source event (schema version 2)

One normalized record from one source. Defined and validated in `src/schema.py`; anything that fails
validation is dead-lettered, never stored.

| Field | Meaning |
|---|---|
| `schema_version` | `2` |
| `event_id` | Stable id: `<source>:<native id>` (NWS CAP identifier, USGS event id, DataSF `row_id`, hash of the RSS guid, Open-Meteo point/type/observation time) |
| `source`, `feed` | Source kind (`nws`, `usgs`, `open_meteo`, `open_data`, `rss`) and the configured feed name |
| `source_event_id`, `source_url` | Native identifier and link back to the record |
| `source_reliability` | `official` (NWS, USGS, police records), `model` (Open-Meteo), `media` (news) |
| `category` | `roadway`, `weather` or `seismic`. Earthquakes are never roadway incidents |
| `type`, `subtype` | e.g. `flooding`/`warning`, `wind`, `accident`/`road_closure`, `earthquake`/`mww` |
| `severity`, `severity_basis` | 1-5 and the rule that produced it (`cap_severity:severe`, `magnitude:6.8`, `gust_kmh:95`, `inferred:headline_keywords`) |
| `title`, `description` | As published (description truncated to 2000 characters) |
| `event_time` | When the hazard started (CAP onset/effective, quake origin time, incident time). For news, the publication time, marked as inferred in `provenance` |
| `published_at` | Source publication or update time |
| `expires_at` | CAP `expires`, or observation time plus the source's window (see lifecycle) |
| `source_status` | CAP message type, USGS review status, `reopened` for news saying a road reopened |
| `references` | CAP identifiers this message updates |
| `series_key` | For repeated observations of one condition at one place (Open-Meteo) |
| `lat`, `lon`, `h3` | Only for point locations; never `0,0`, never a polygon centroid |
| `geometry` | GeoJSON Polygon/MultiPolygon for area events |
| `location` | `confidence`, `precision`, `place`, `region`, `evidence` (see below) |
| `road`, `direction` | Extracted from text where present |
| `attributes` | Source-specific values kept verbatim (CAP certainty/urgency/onset/ends/UGC, magnitude/depth/PAGER alert, model readings with units, outlet) |
| `provenance` | Which fields are source-provided, inferred or computed, and how |

`first_observed` (when SAURON first fetched it) and `stored_at` are kept as store columns, not as part of the
event content, so re-polls do not create versions.

**Versioning.** A new version is stored only when the content hash changes. The hash covers type, severity,
title, description, location, geometry, road, direction, status and references, but not observation times or
expiry, which are updated in place.

## Location confidence

These are categories decided by explicit rules, not probabilities.

| Value | Rule | Mapped |
|---|---|---|
| `source_point` | Coordinates published by the source | yes, as verified |
| `source_area` | Polygon published by the source or its NWS forecast zones | yes, as polygons |
| `geocoded_high` | Place named in the text and its state named in the text (`Topanga, CA`, `in Greenville County, South Carolina`) | yes, as approximate |
| `geocoded_medium` | Place named in the text; state chosen from the outlet's market, another state mention, or because the name is unique nationally | yes, as approximate |
| `geocoded_low` | Place named in the text but matching several places; no coordinates stored | no |
| `unresolved` | No usable place in the text, outside US coverage, or no geometry available | no |

`precision` is `point`, `area`, `city`, `county`, `state` or `none`. `evidence` records the matched gazetteer
entry, how the state was chosen, the number of candidates and, if unresolved, why.

The outlet's market is never used as the location. It only picks among same-named places that the headline
itself mentions ("crash in Canton" from a Watertown, NY station → Canton, NY). Headlines that name no place stay
unresolved even when the outlet is known.

## Incident

The current view of one real-world hazard, built from one or more source events (`incidents` table). Fields are
recomputed from the latest version of every attached event:

- **Primary evidence**: the most reliable source, then the most recent. It provides category, type, title and
  description. The best-located evidence provides the location.
- **Severity**: latest primary value for NWS/Open-Meteo/USGS (updates can downgrade); maximum across reports
  for news.
- **Verification**: `official` if any official source; else `model_estimate` if any model source; else
  `corroborated` if two or more distinct news outlets; else `unverified`.
- `sources`, `evidence_count`, `event_time` (earliest), `first_observed`, `last_updated`, `expires_at`.

### Lifecycle

`open` → `updated` → `cleared`, recorded in `incident_history` with the triggering event.

| Clear reason | When | What it does not mean |
|---|---|---|
| `source_expired` | NWS message passed its `expires` time without an update | The hazard may still be present |
| `source_withdrawn` | NWS stopped listing the alert before it expired (cancelled or replaced) | |
| `not_reobserved` | Open-Meteo observations stopped showing the condition (45-minute window) | |
| `observation_window_elapsed` | No new reports within the window: 6 h for news and DataSF, 24 h for earthquakes | The road may still be closed |
| `reported_reopened` | A later news report says the road reopened | Unverified |

A cleared incident reopens if new evidence arrives that has not expired (an NWS update extending an alert), or
if an alert cleared as `source_withdrawn` appears in a later NWS snapshot (history reason `listed_again`), so a
momentary gap in the API's active list does not leave a live warning cleared.
DataSF records arrive 1-3 days late and are stored directly as cleared history, never shown as active.

### Correlation

Deterministic, in order:

1. Same `event_id` (a new version of a known record).
2. Source references: CAP `references` for NWS updates, alternate ids for USGS.
3. Same `series_key` while the incident is open (Open-Meteo condition at one point). An unchanged severity only
   extends expiry; a change adds evidence.
4. Roadway events with a point or city location: open incidents of the same type in the same or neighbouring
   H3 cell (resolution 7) within ±180 minutes, rejected if both name different roads or opposite directions.
   City- or county-level locations also need headline token overlap ≥ 0.3 (Jaccard) or the same road, because
   every report placed at a city centroid shares that point.

Weather and seismic events are not merged across sources.

## Store tables

`source_events` (every version, full JSON payload, bbox columns for area search), `incidents`,
`incident_history`, `feed_status`, `dead_letters`, `cache` (NWS zone geometry, optional Nominatim), `meta`.
