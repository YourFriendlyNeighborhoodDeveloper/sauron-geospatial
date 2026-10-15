# Operations

## Ways to run

| Mode | Command | Needs |
|---|---|---|
| Offline demo | `python -m src --demo` | Python only. Replays bundled fixtures into `data/demo.db` |
| Local, no broker | `SAURON_TRANSPORT=local python -m src` | Network to the sources. Not durable, no replay |
| Local with Kafka | `docker compose up -d kafka kafka-init` then `python -m src` | Docker |
| Full stack | `docker compose up -d --build` | Docker. Dashboard on 127.0.0.1:8050, Prometheus :9090, Grafana :3000 |

Run `python -m tools.fetch_gazetteer` once before a live local run (the container does it on start). Without
it, news headlines stay unresolved and a warning is logged.

## Configuration

`config/config.yaml` holds everything that is not secret. The environment overrides it:

| Variable | Purpose |
|---|---|
| `KAFKA_BOOTSTRAP_SERVERS` | Broker address (default `127.0.0.1:9092`) |
| `SAURON_TRANSPORT` | `kafka` or `local` |
| `SAURON_DB_PATH`, `SAURON_REPORTS_DIR` | Store and output locations |
| `SAURON_HOST`, `SAURON_PORT` | Dashboard bind address (default `127.0.0.1:8050`) |
| `SAURON_METRICS_HOST`, `SAURON_METRICS_PORT` | Prometheus endpoint (default `127.0.0.1:8000`) |
| `SAURON_LOG_LEVEL`, `SAURON_LOG_FORMAT` | `json` (default) or `text` |
| `SAURON_CONTACT_EMAIL` | Appended to the User-Agent; NWS and Nominatim ask for a contact |
| `SAURON_DASHBOARD_USER`, `SAURON_DASHBOARD_PASSWORD` | HTTP Basic auth for the dashboard and API |
| `SAURON_API_TOKEN` | Bearer token for the API |
| `SAURON_ALLOW_UNAUTHENTICATED` | `1` to serve on a non-loopback address without credentials (see below) |
| `OPEN_METEO_API_KEY` | Paid Open-Meteo key; switches to the commercial endpoint |

Copy `.env.example` to `.env` for Docker Compose. `.env` is git-ignored.

## Security model

- Every port Compose publishes is bound to `127.0.0.1`. Kafka, Prometheus and the metrics endpoint are not
  authenticated and must stay on loopback or a private network.
- The app refuses to listen on a non-loopback address unless a dashboard password or API token is set, or
  `SAURON_ALLOW_UNAUTHENTICATED=1`. Compose sets the latter because the container must bind `0.0.0.0` for Docker's
  port mapping, which is itself loopback-only. Set a password and change that to `0` before publishing the port
  anywhere else, and put TLS in front (a reverse proxy); the app does not terminate TLS.
- Credentials are compared in constant time. `/healthz` is the only unauthenticated path when auth is on.
- Grafana runs with anonymous read-only access and both login and basic auth disabled, so no default admin
  password is usable.
- The container runs as an unprivileged user.

## Health and monitoring

- `GET /healthz`: process is up.
- `GET /readyz`: store reachable, per-feed status, counts. Feed status is `ok`, `stale` (no success for 3 poll
  intervals + 60 s), `failing` (3+ consecutive failures), `never_succeeded` or `unknown`. A successful poll with
  zero items is `ok`: quiet sources are not errors.
- Dashboard "System health" tab: the same feed table, store counts and recent dead letters.
- Prometheus metrics (`src/metrics.py`): poll outcomes, last success time, source response time, items received,
  events normalized/rejected/filtered (with reasons), duplicates suppressed (feed, store, re-observation),
  geocoder outcomes, dead letters, publish errors, active incidents, consumer lag, processing time, and
  publication-to-available latency (first versions processed within 10 minutes of fetching).
- Alert rules (`monitoring/alerts.yml`): feed stale or failing, consumer lag, dead letters, publish errors. None
  of them fire on zero events.
- Grafana dashboard "SAURON pipeline" is provisioned automatically.

## Map

The Map tab uses Leaflet 1.9.4 (with Leaflet.markercluster and Leaflet.heat), vendored in `src/assets/` with their
licenses in `src/assets/vendor-licenses/`. It draws with SVG and Canvas, so it does not need WebGL; the earlier
Plotly/MapLibre map rendered as an empty dark panel in browsers without WebGL.

Basemaps (switch with the layer control): USGS Topo (default) and USGS Imagery from The National Map (public domain,
no key), OpenStreetMap, and a dark variant of OpenStreetMap made with a CSS filter. OpenStreetMap's tile servers
allow light use under the OSMF tile usage policy; host tiles yourself for anything heavier. CARTO's raster tiles,
used by many examples, now return an "API key required" placeholder and are not used.

If no tile loads (offline, firewall), the map shows a warning and keeps drawing incidents and warning areas.
Incidents with an ambiguous or unresolved location are never placed on the map; the legend counts them as
"without a reliable location".

## Logs

JSON lines on stderr. Fields passed with a log call are top-level keys, e.g.
`{"level": "WARNING", "msg": "feed poll failed", "feed": "nws_alerts", "error_category": "http_503", ...}`.

## Replaying the raw log

After changing a mapper, rebuild the store from Kafka's retained raw events (14 days by default):

```sh
docker compose stop app                       # or stop python -m src
mv data/sauron.db data/sauron.db.bak          # keep the old store until the new one is checked
docker compose exec kafka /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server kafka:29092 \
  --group sauron-processor --topic hazards.raw --reset-offsets --to-earliest --execute
docker compose start app
```

Zone geometries are refetched on demand. Replays are idempotent; replaying into an existing store only adds
versions whose content changed.

## Backups

The store is one SQLite file. `sqlite3 data/sauron.db ".backup data/sauron-backup.db"` is safe while the app runs.
Reports and the CSV are derived and can be regenerated.

## Legacy output from the original pipeline

`python -m tools.migrate_legacy_reports --move-originals` handles `reports/hazards.csv` and `reports/*.txt`
written by the original code:

1. Writes `reports/legacy/backup-<timestamp>.tar.gz` with a SHA-256 manifest and verifies every member.
2. Splits the CSV by row shape (the original header had 14 columns; most rows had 18), de-duplicates by id, and
   marks rows `legacy_status=unvalidated`. Rows of any other shape go to `quarantine.csv` with line numbers.
3. Moves the originals to `reports/legacy/original/` only after the backup is verified.
4. Writes `reports/legacy/manifest.json` with the counts.

Run on 2026-10-15: 6,502 files backed up; CSV rows 3,604 (14-column) + 18,451 (18-column) reduced to
1,895 + 4,753 unique; 0 quarantined. Legacy rows are not imported into the store: their coordinates came from
the old geocoder (see `docs/data-quality.md`) and their text had commas replaced with spaces, which cannot be
undone.

## Troubleshooting

- **News incidents all unresolved**: the gazetteer is missing. Run `python -m tools.fetch_gazetteer`.
- **NWS alerts appear a few polls late**: alerts wait for their zone geometries, at most 40 zone requests per
  poll and 10 polls per alert.
- **`ECONNREFUSED ::1` from the Kafka client**: `localhost` resolved to IPv6 while the broker listens on IPv4. Use
  `127.0.0.1`.
- **Original images no longer pull**: Compose uses the official `apache/kafka` image. Bitnami moved its versioned
  images to `bitnamilegacy` in August 2025; on 2026-10-15 Docker Hub no longer served a manifest for
  `bitnami/kafka:3.7`, which the original compose file used.
