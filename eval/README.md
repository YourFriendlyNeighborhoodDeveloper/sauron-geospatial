# Geocoding and classification evaluation

| File | Contents |
|---|---|
| `geo_labels_dev.jsonl` | 157 labeled headlines, examined while building the resolver (optimistic) |
| `geo_labels_holdout.jsonl` | 78 labeled headlines, labeled after the code was frozen |
| `holdout_frozen_code.sha256` | Hashes of `src/geocoding.py`, `src/hazard_enrichment.py`, `config/outlets.yaml` recorded before the holdout was viewed |
| `results/geo_eval_*.json` | Output of `tools/eval_geo.py`, with per-headline outcomes |
| `results/benchmark.json` | Output of `tools/benchmark.py` |

Each row has the headline title, outlet, publication time, the coordinates the original pipeline stored
(`baseline`), a class label (`incident`, `followup`, `other`) and a truth label:

- `{"scope": "place", "kind": "place|town|county", "name": ..., "state": ...}`: resolved through the Census
  Gazetteer; `near` picks between same-named places in one state
- `{"scope": "state", "state": ...}`: only the state can be determined
- `{"scope": "non_us"}` or `{"scope": "unknown"}`

**Labeling protocol.** The headlines are from the original pipeline's output (public news headlines). They were
split 80/20 by SHA-256 of the lower-cased title before the new resolver existed; the outlet table was built from the
80% split only. Labels record where the incident happened, as determinable from the headline plus the outlet's
known broadcast market, assigned by hand without opening the articles. Uncertain cases were labeled `state` or
`unknown` rather than guessed. Headlines naming individuals were excluded. In the original CSV, commas in titles had
been replaced with spaces; double spaces were restored to ", ".

```sh
python -m tools.fetch_gazetteer
python -m tools.eval_geo --labels eval/geo_labels_holdout.jsonl --json eval/results/geo_eval_holdout.json
python -m tools.eval_geo --labels eval/geo_labels_holdout.jsonl --no-outlets
```
