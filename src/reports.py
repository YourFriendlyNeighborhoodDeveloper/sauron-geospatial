import csv
import hashlib
import io
import os
import tempfile

from .schema import CLEAR_REASONS, SOURCE_RELIABILITY

CSV_COLUMNS = (
    "incident_id", "state", "clear_reason", "category", "type", "subtype", "severity", "verification",
    "location_confidence", "location_precision", "place", "region", "lat", "lon", "h3", "road", "direction",
    "title", "sources", "evidence_count", "event_time", "first_observed", "last_updated", "expires_at",
    "cleared_at", "schema_version",
)


def atomic_write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def incidents_csv(rows):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow(["" if row.get(c) is None else row.get(c) for c in CSV_COLUMNS])
    return buf.getvalue()


def render_incident(incident, evidence):
    lines = [
        f"Incident: {incident['incident_id']}",
        f"Title: {incident['title']}",
        f"Category / type: {incident['category']} / {incident['type']}" + (f" ({incident['subtype']})" if incident.get("subtype") else ""),
        f"Severity: {incident['severity']} / 5",
        f"State: {incident['state']}",
    ]
    if incident["state"] == "cleared":
        lines.append(f"Cleared: {incident['cleared_at']} - {CLEAR_REASONS.get(incident['clear_reason'], incident['clear_reason'])}")
    lines += [
        f"Verification: {incident['verification']}",
        f"Event time: {incident['event_time']}",
        f"First observed by SAURON: {incident['first_observed']}",
        f"Last source update: {incident['last_updated']}",
        f"Expires: {incident['expires_at'] or 'n/a'}",
        f"Location: {incident['location_confidence']} ({incident['location_precision']})",
        f"Place: {incident.get('place') or 'unresolved'}" + (f", {incident['region']}" if incident.get("region") else ""),
    ]
    if incident.get("lat") is not None:
        lines.append(f"Coordinates: {incident['lat']:.5f}, {incident['lon']:.5f}")
    if incident.get("road"):
        lines.append(f"Road: {incident['road']}" + (f" ({incident['direction']})" if incident.get("direction") else ""))
    lines += ["", f"Evidence ({len(evidence)} source records):"]
    for e in evidence:
        ev = e["event"]
        lines.append(f"- [{ev['source']}/{ev['source_reliability']}] {ev['title']}")
        lines.append(f"  id={ev['event_id']} version={e['version']} event_time={ev['event_time']} "
                     f"published={ev.get('published_at') or 'n/a'}")
        if ev.get("source_url"):
            lines.append(f"  url={ev['source_url']}")
        basis = ev["location"].get("evidence") or {}
        if basis:
            lines.append("  location basis: " + ", ".join(f"{k}={v}" for k, v in sorted(basis.items()) if v not in (None, "", [])))
    lines += ["", "Source reliability: " + "; ".join(f"{k} = {v}" for k, v in SOURCE_RELIABILITY.items())]
    return "\n".join(lines) + "\n"


class Reports:
    def __init__(self, store, directory):
        self.store = store
        self.dir = os.path.join(directory, "incidents")
        self.csv_path = os.path.join(directory, "incidents.csv")

    def write_incident(self, incident_id):
        con = self.store.connect()
        incident = self.store.read("SELECT * FROM incidents WHERE incident_id = ?", (incident_id,))
        if not incident:
            return False
        incident = incident[0]
        text = render_incident(incident, self.store.evidence(con, incident_id))
        digest = hashlib.sha256(text.encode()).hexdigest()
        if digest == incident.get("report_hash") and os.path.exists(self._path(incident_id)):
            return False
        atomic_write(self._path(incident_id), text)
        with self.store.transaction() as tx:
            tx.execute("UPDATE incidents SET report_hash = ? WHERE incident_id = ?", (digest, incident_id))
        return True

    def export_csv(self):
        rows = self.store.read(f"SELECT {', '.join(CSV_COLUMNS)} FROM incidents ORDER BY event_time, incident_id")
        atomic_write(self.csv_path, incidents_csv(rows))
        return len(rows)

    def _path(self, incident_id):
        return os.path.join(self.dir, f"{incident_id}.txt")
