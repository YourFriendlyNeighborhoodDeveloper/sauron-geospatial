# Data quality: baseline and measured results

All numbers were measured on 2026-10-15 on an Apple-silicon Mac (10 cores, Python 3.12.8) and are reproducible
with the commands shown. Anything not measured is marked as such.

## Baseline (original code)

Reproduced by running the original modules on live responses captured that day, and by analysing the original
`reports/hazards.csv`.

| Defect | Evidence |
|---|---|
| NWS produced nothing | Original `NWSFeed.parse` on the live response: **0 of 525** active alerts; the `TypeError` from double decoding was swallowed |
| Wind false positives | 20 km/h gust (12 mph) produced "Gusts 20.0 m/s" wind hazard; 1,343 of 1,509 legacy weather hazards were wind |
| Fog never fired | 300 m visibility produced no fog hazard (metres compared with a 1.0 km threshold) |
| Snow from temperature | Dry −2 °C produced "snow"; 136 legacy hazards |
| DataSF lost coordinates and matched nothing | Original parser returned `(None, None)` for a row with coordinates; category `'Vehicle Incident'` does not exist |
| SFPD blotter | No `parse()`; site now behind a consent wall; 0 records ever |
| Replay duplication | 18,451 CSV rows for 4,753 unique hazards (3.9×; one hazard appeared 32 times) |
| CSV corruption | 14-column header; 84% of rows had 18 columns |
| Placeholder coordinates | 11 legacy rows at `0.0, 0.0` |
| Tests | 19 regex-extractor tests; `h3` was missing from the local environment, so the main module could not be imported outside Docker |

## Geocoding of news headlines

**Method.** Unique legacy headlines were split deterministically (SHA-256 of the title, 1 in 5 to "test") before any
new code was written. The outlet-market table (`config/outlets.yaml`) was built only from the other 80%. From the
test split:

- **Dev set** (157 headlines, `eval/geo_labels_dev.jsonl`): labeled and looked at while building the resolver.
  Its numbers are optimistic.
- **Holdout** (78 headlines, `eval/geo_labels_holdout.jsonl`): labeled only after the resolver and classifier code
  were frozen; file hashes recorded in `eval/holdout_frozen_code.sha256` before the headlines were displayed. Two
  later bug fixes (title-case threshold, GeometryCollection support) changed those files; re-scoring the holdout
  with the final code gave identical numbers.

Labels are the incident location as determinable from the headline and the outlet's known market, assigned by
hand without opening the articles. Headlines naming individuals were excluded. "Correct" means the system's
point is within 25 km of the labeled city/town (60 km for counties); for state-only labels, a point in another
state is wrong; for non-US stories, any point is wrong. The "baseline" is the coordinates the original pipeline
stored for the same headlines.

```sh
python -m tools.fetch_gazetteer
python -m tools.eval_geo --labels eval/geo_labels_holdout.jsonl
```

**Holdout results (78 headlines, 58 with a known place):**

| | Original | Current |
|---|---|---|
| Headlines given coordinates | 47 | 23 |
| Wrong placements | 23 (49% of placements) | 1 (4%) |
| Correct placements (of 58) | 19 (33%) | 22 (38%) |
| Left unresolved (of 58) | 19 | 35 |
| Non-US stories placed in the US | 0 of 3 | 0 of 3 |

Without the outlet-market table the current resolver places 12 and gets 11 right: the table doubles correct
placements without adding wrong ones.

**Dev set (157 headlines, 117 with a known place):** original 32 wrong of 93 placed, 54 correct; current 0 wrong
of 62 placed, 62 correct.

**Reading these results.** The main gain is removing wrong placements, from about half of all placements to one
in 23, not coverage: 35 of 58 located headlines stay unresolved. Most of them name no place
("crash on I-77"), name it without a preposition ("Fremont truck accident"), or name an ambiguous place from an
outlet not in the table. The remaining holdout error is a county match ("San Bernardino County" for a crash in
Ontario, CA) whose county centroid is about 150 km from the crash: county-level points are coarse for large
counties.

## News classification (incident vs follow-up vs other)

| Set | Incident precision | Incident recall | Original precision (kept everything) |
|---|---|---|---|
| Holdout (78) | 0.947 | 1.000 | 0.910 |
| Dev (157, tuned on) | 0.993 | 0.986 | 0.898 |

## Weather rules

Regression tests (`tests/test_open_meteo.py`) pin the corrected behaviour: gusts of 18.5-71.9 km/h, visibility of
21.2 km and dry −4 °C produce no hazard; 80 km/h → wind 3, 100 km/h → wind 4; 350 m → fog 3; freezing-rain code
67 → ice 4.

The legacy descriptions record the raw readings, so every legacy weather hazard can be re-checked against the
corrected rules (`reports/legacy/hazards_legacy_v1.csv`):

| Legacy hazard | Count | Still a hazard under corrected rules |
|---|---|---|
| Wind | 1,343 | 8 (0.6%); median recorded gust was 29 km/h (18 mph), range 18-96.5 km/h |
| Snow | 136 | 0: all were "Freezing temps" with no precipitation recorded |
| Ice | 28 | 21: the other 7 were at 0-1.5 °C |

This compares old output with the new rules, not with what actually happened. A false-positive rate against
observed conditions was **not measured**: no labeled ground truth from station observations was collected.

## Replay correctness

- Processing the fixture log twice into one store changes nothing, including report file timestamps.
- Processing it into two fresh stores with the same clock produces identical events, incidents, history and
  report text.
- Against real brokers (`tests/integration`; passed on the Compose stack's `apache/kafka:3.9.1` and on a
  Kafka 3.7 broker): offsets committed only after storage; restarting with
  the same group reprocessed 0 messages; republishing the full log added 0 rows; replaying everything under a new
  group left the store unchanged.
- Live restarts on 2026-10-15: feeds republish everything after a restart. Host process: 133 repeats suppressed by
  the store, 5 new alerts stored. Compose deployment (`docker compose restart app`, data on the named volume):
  136 repeats suppressed, 4 new events stored (137 → 141), no duplicates.

## Throughput (processing only)

`python -m tools.benchmark --copies 200` (4,400 messages: the fixture mix with unique ids, on-disk SQLite, full
gazetteer, no network or Kafka), measured while a live pipeline was also running on the machine:

| Pass | Messages/s | p50 | p95 | max |
|---|---|---|---|---|
| First processing | 785 | 0.57 ms | 9.5 ms | 24 ms |
| Replay (all duplicates) | 2,422 | 0.10 ms | 1.6 ms | 24 ms |

Source volume is far below this: the busiest legacy day had 482 events.

## Live run

Docker Compose deployment (`apache/kafka:3.9.1`, real sources), 2026-10-15 17:47-17:58 UTC, plus a 14-minute
host-process run before it. These are short windows: they show the system working on live data, not long-term
reliability.

| | Compose window (11 min) |
|---|---|
| Feed polls | 17, all successful (NWS 8, USGS 4, news 2, Open-Meteo 2, DataSF 1); 0 feed errors |
| Source events stored | 178, 0 rejected, 0 dead letters |
| Filtered as out of scope | 49 NWS non-road products, 7 sub-M2.5 quakes, 11 calm Open-Meteo points, 8 news items (3 follow-ups, 3 non-incidents, 2 non-US) |
| Duplicates suppressed | 842 at the feeds, 136 in the store (after a container restart) |
| Incidents | 72 open weather (NWS polygons), 3 open seismic, 14 open/updated roadway, 86 cleared (85 news/DataSF items past their window, 1 expired NWS alert) |
| Roadway location confidence | 6 source points (DataSF), 2 geocoded high, 31 medium, 15 low, 45 unresolved |
| Correlation seen live | 1 NWS update joined its original alert via CAP references; 1 crash corroborated by two outlets |
| Publication → available | NWS: n=6, median 67 s, max 114 s (60 s polling); USGS: n=1, 49 s. Computed from the store for records published after start |
| Processing time per message | about 0.6 ms median, 20 ms p95 (Prometheus histogram estimate) |

Host-process run (14 min, Kafka 3.7): every poll of every feed succeeded; two new NWS alerts were queryable 42 s
after publication.

## Not measured

- Precision of cross-source news correlation (merges were checked on fixtures only).
- Weather false-positive rate against observations.
- Behaviour beyond one broker partition or one process.
- Multi-day operation. The live runs below cover about an hour in total.
