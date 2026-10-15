# Design: Property & Storm Intelligence (next stage)

Status: design only. Nothing in this document is implemented beyond what is marked as existing. No billing,
accounts or production deployment are in scope here.

## Problem

Roofers, public adjusters, claims teams and attorneys need to answer one question with evidence they can cite:
*what severe weather affected this property during this period?* Today they assemble it by hand from NWS
products, storm reports and radar viewers.

## Customer workflow

1. A user submits an address and a date range.
2. The system geocodes the address and states how well it matched.
3. It collects every relevant record for that point and window, keeps each evidence class separate, and states
   what each one can and cannot show.
4. It returns a report that can be traced: every statement links to a source record with its identifier, URL and
   retrieval time, and the report records the data versions it was built from.

## Evidence classes

The report never merges these classes into one score, and it never states that damage occurred.

| Class | Example sources | What it shows | What it does not show |
|---|---|---|---|
| **Warnings and advisories** | NWS CAP alerts with polygons or zones (SAURON ingests these today) | Forecasters expected or detected a hazard over an area during a period | That the hazard reached this property, or any damage |
| **Ground reports** | NWS Local Storm Reports; SPC preliminary storm reports (hail size, measured or estimated gusts, damage reports with time and location) | A trained spotter, official or member of the public reported a hazard at a point | Conditions at a different point; reports are preliminary and sometimes mislocated |
| **Instrument observations** | ASOS/AWOS station peak gusts | Measured wind at the station | Wind at the property, which may be kilometres away |
| **Radar estimates** | NOAA MRMS products such as MESH (estimated maximum hail size) | Radar-derived estimate over a grid cell | Hail that actually fell; estimates can be wrong in both directions |
| **Damage evidence** | Inspections, photos, claims (customer-supplied) | Physical damage | Its cause or timing, unless the record establishes them |

Each item carries its distance from the property, its time relative to the window, and a plain statement of its
class's limits.

## Report contents

- Property: submitted address, matched address, coordinates, match quality and geocoder
- Window: requested range with time zone
- Applicable warnings: each NWS product whose polygon contains the point (exact point-in-polygon, as in
  `GET /api/v1/alerts/at` today), with issuance, expiry, all updates, CAP severity/certainty/urgency and a link
- Reported events: storm reports within a stated radius (e.g. 10 km) with distance, time, reported magnitude and
  source
- Observations: nearest stations' peak gusts with distance
- Radar estimates: maximum MESH value over the property's grid cells during the window, marked as an estimate
- Source attribution and licenses
- Limitations: the standard statements above, plus any data gaps (source outage, archive missing)
- Generation timestamp, report id, hash of inputs, software version

## Data sources and licensing

Built on US Government data (public domain), so it can be sold without per-record licensing:

- NWS alerts: archived history is needed for date ranges before SAURON started collecting. Candidate: the Iowa
  Environmental Mesonet archive of NWS warnings and Local Storm Reports (derived from NWS products; confirm its
  terms and attribution request before use).
- SPC storm reports: daily files published by the Storm Prediction Center.
- MRMS: NOAA's Multi-Radar/Multi-Sensor products, published through NOAA's open data program (GRIB2; needs
  decoding).
- Address geocoding: the US Census Geocoder (free, public domain) or a self-hosted Nominatim/Pelias with OSM
  attribution. The public Nominatim server's policy does not allow this use at volume.

Excluded from the commercial product: Open-Meteo's free tier (non-commercial), Google News headlines (not licensed
for redistribution), and any personal data about property owners.

## What exists today

| Capability | Status |
|---|---|
| NWS alert ingestion with polygons and zone geometry, updates linked through CAP references | Implemented, tested |
| Every alert version stored with provenance | Implemented |
| Point-in-polygon query over stored alerts for a time window | Implemented (`/api/v1/alerts/at`) |
| Incident lifecycle and explicit expiry semantics | Implemented |
| Feed health, dead letters, replay | Implemented |
| Historical backfill of NWS alerts | Not implemented; only alerts collected since start are searchable |
| Storm reports, station observations, MRMS | Not implemented |
| Address geocoding | Not implemented (place-name gazetteer only) |
| Report rendering (PDF), report versioning | Not implemented |
| Accounts, auth beyond a shared token, audit log, billing | Not implemented |

## Architecture changes

- **Request-driven, not streaming.** Reports are built on demand from archives. Kafka stays for live alert
  ingestion but is not on the report path.
- **Storage.** A few years of nationwide warnings is hundreds of thousands of polygons. SQLite's built-in R*Tree
  module can replace the bbox columns first; move to PostGIS when there are concurrent writers or heavy spatial
  joins (storm-report radius queries, MRMS cell lookups).
- **Ingestion jobs** for archives (daily SPC files, LSRs, MRMS subsets clipped to the US and to the hail products
  only), each with fixtures, schema validation and source health, like the existing feeds.
- **Report service:** a pure function from (address match, window, data versions) to a report document, so the
  same inputs always give the same report. Store the inputs hash and the rendered output.
- **Auditability:** append-only report log; reports reference immutable source record versions (already true of
  `source_events`).

## Deployment economics

A pilot fits on one small VM (2 vCPU, 4 GB RAM) running the app, SQLite or a small Postgres, and a single Kafka
broker, or no Kafka at all if live alerts are dropped for the pilot. Typical cost: a VM in the low tens of US
dollars per month, plus object storage for MRMS subsets (hail products for the US are a small fraction of the
full MRMS archive). There are no mandatory paid APIs. Check current provider prices before committing; these are
order-of-magnitude estimates, not quotes.

## Paid pilot: technical plan

1. Backfill 3 years of NWS warnings and storm reports for one region (e.g. a few states in hail country); verify
   counts against the source archives.
2. Add the Census geocoder with match-quality reporting; test on 200 real, consented addresses.
3. Add SPC/LSR ingestion and the radius query; add MRMS MESH for the same region.
4. Build the report: JSON first, then PDF; golden-file tests so identical inputs produce identical reports.
5. Validate with pilot users: for 50 past claims where the outcome is known, compare report evidence with what the
   adjuster found. Report agreement and disagreement honestly; this is the product's real accuracy test.
6. Exit criteria: users can cite the report without manual cross-checking in at least N of 50 cases (N agreed
   with the pilot customer), and no statement in any report overstated its evidence class.

## Risks

- **Overclaiming:** the largest risk. Wording is fixed per evidence class and reviewed; warning coverage is never
  called damage.
- **Data gaps:** archive outages and late corrections. Every report lists gaps and source retrieval times.
- **Location error:** address geocoding can be off by a parcel or more; the report shows match quality and
  distance-based statements, not just yes/no.
- **Name:** "Sauron" is a character from Tolkien's works. The Tolkien estate's licensing arm actively enforces
  its trademarks, so a commercial product should use a different name.
- **Privacy:** submitted addresses are customer data. Define retention, never enrich them with owner information,
  and keep them out of logs.
