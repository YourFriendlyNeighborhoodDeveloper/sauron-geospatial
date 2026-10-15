# Sources

| Feed | Endpoint | License / terms | Reliability | Fixture |
|---|---|---|---|---|
| `nws_alerts` | `api.weather.gov/alerts/active?status=actual&message_type=alert,update&region_type=land` + zone geometry | US Government work, public domain; NWS asks for an identifying User-Agent | official | `tests/fixtures/nws_active.json`, `nws_zones/` |
| `usgs_quakes` | `earthquake.usgs.gov/.../summary/all_hour.geojson` | Public domain | official | `usgs_feed.json` |
| `sf_traffic_collisions` | DataSF `wg3w-h783` (SFPD incident reports), `incident_category = 'Traffic Collision'` | DataSF open data (PDDL) | official, delayed | `datasf_traffic_collisions.json` |
| `open_meteo` | `api.open-meteo.com/v1/forecast` (`customer-api.open-meteo.com` with a key) | CC BY 4.0; **free tier is non-commercial only** | model | `open_meteo_calm.json` |
| `news_rss` | Google News RSS search | Headlines remain the publishers'; not licensed for commercial redistribution | media | `news_rss.xml` (fictional sample in the real format) |

Every active source has a parser that rejects malformed responses with a `parse` error, a mapper to the
canonical schema, fixture-based tests, and health reporting.

## NWS

- One nationwide request replaces the original 50 per-state requests a minute. The original parser called
  `json.loads` on already-decoded objects; the `TypeError` was swallowed and **0 of 525 active alerts** were
  ever produced (reproduced against a live response on 2026-10-15).
- Conditional requests with ETag; 429/5xx retried with backoff honouring `Retry-After`.
- Only the products listed under `feeds.nws_alerts.events` in `config/config.yaml` become hazards (warnings,
  watches and advisories for flooding, winter weather, wind, fog, smoke, dust, tropical systems, tornadoes,
  severe thunderstorms, fire weather, avalanches). Freeze, heat, air-quality and marine products are filtered
  with reason `event_not_road_relevant`.
- After each successful poll a snapshot of active alert ids is published; open alerts that disappear before
  expiring are cleared as `source_withdrawn`.
- Preserved: CAP id, event, severity, certainty, urgency, headline, description, sent, effective, onset,
  expires, ends, geometry (alert polygon or merged zones), area description, UGC codes, sender, references.

## Open-Meteo

- Units are requested explicitly (`wind_speed_unit=kmh`, `precipitation_unit=mm`, Celsius) and checked against
  `current_units`; a mismatch rejects the observation.
- The original code compared km/h gusts with an 18 m/s threshold (any gust above 18 km/h, about 11 mph, became a
  wind hazard), compared visibility in metres with a 1 km threshold (fog could only fire below 1 m), and called
  every dry reading at or below 0 °C snow. In the legacy output, 1,343 of 1,509 weather hazards were wind and 136
  were "snow" from temperature alone.
- Current rules, configurable under `feeds.open_meteo.thresholds`: gusts ≥ 72 km/h (NWS wind advisory level,
  severity 3) or ≥ 93 km/h (high wind warning level, severity 4); visibility ≤ 400 m, or fog codes 45/48 with
  ≤ 1000 m; freezing drizzle/rain codes or liquid precipitation at ≤ 0 °C; snow codes or measured snowfall
  (≥ 2.5 cm/h is heavy); precipitation ≥ 7.6 mm/h; thunderstorm codes. Amounts per 15-minute interval are
  converted to hourly rates.
- Conditions at a point are one incident while they persist. Unchanged severity extends the incident instead of
  storing a new event.
- Commercial use requires a paid key (`OPEN_METEO_API_KEY`) or disabling the feed.

## USGS

- Magnitude, magnitude type, depth, place, status, tsunami flag, felt reports, MMI, PAGER alert, significance,
  network and alternate ids are kept.
- Events below `min_magnitude` (2.5) are filtered; in the original output most stored "hazards" were micro-quakes,
  131 of them outside the US.
- Category `seismic`, never `roadway`: a quake is not a road closure. A 24-hour observation window applies.

## DataSF traffic collisions

- The original query asked for category `'Vehicle Incident'`, which does not exist, over the last hour of a
  dataset published 1-3 days late, and read coordinates from keys the GeoJSON `point` does not have. It never
  produced a record.
- Now: `incident_category = 'Traffic Collision'` over the last 7 days, coordinates from `latitude`/`longitude`,
  local times converted from America/Los_Angeles. Records are historical and stored as cleared.

## News RSS

- Headlines are classified before anything else: follow-ups (victim identified, charges, mourning, later
  developments) and non-incidents (law-firm marketing, statistics roundups) are filtered. Measured on held-out
  labeled headlines: incident precision 0.947, recall 1.0 (`docs/data-quality.md`).
- Filtering follow-ups also keeps most headlines that name victims out of the store.
- Locations come from the text only, via the gazetteer (`docs/data-model.md`). Non-US stories are filtered as
  `outside_coverage`.
- News reports are unverified signals. Two outlets reporting the same crash make an incident `corroborated`.

## Removed sources

The original sources remain in the baseline commit (`31e292e`):

| Source | Why removed |
|---|---|
| SFPD news blotter scraper | `sfpolice.org/news` now returns a JavaScript consent-manager page; the scraper also had no `parse()` and never produced a record |
| Waze, TomTom, 511, Citizen | Never registered in the feed factory; constructors did not match `BaseFeed`; need keys or partner access; terms restrict commercial use |
| Police scanner speech-to-text, Reddit/social, EMS | Not registered; dependencies not installed; EMS was a stub |
| Person-level contact enrichment | Looked up contact details for people named in crash reports. Removed: SAURON keeps event-level data only |

## Geocoding reference data

US Census Bureau 2025 Gazetteer files (places, county subdivisions with active governments, counties): public
domain, downloaded by `python -m tools.fetch_gazetteer` into `data/`. Nominatim (OpenStreetMap, ODbL) is an
optional fallback, off by default; when enabled it is limited to one request per 1.1 s, cached for 30 days, and
only accepted if the result is in the expected state. Point `base_url` at a self-hosted instance for any real
volume.
