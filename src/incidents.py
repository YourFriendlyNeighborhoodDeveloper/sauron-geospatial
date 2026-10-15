import hashlib
import json
import re
from datetime import timedelta

from . import geo
from .metrics import incident_transitions
from .schema import SCHEMA_VERSION, parse_time, utc_iso

RELIABILITY_RANK = {"official": 0, "model": 1, "media": 2}
LOCATION_RANK = {"source_point": 0, "source_area": 1, "geocoded_high": 2, "geocoded_medium": 3,
                 "geocoded_low": 4, "unresolved": 5}
LATEST_WINS = {"nws", "open_meteo", "usgs"}
CLEAR_REASON_BY_SOURCE = {"nws": "source_expired", "open_meteo": "not_reobserved"}
STOPWORDS = {"the", "and", "after", "with", "for", "on", "in", "at", "of", "near", "to", "from", "says",
             "police", "crash", "vehicle", "multi", "two", "three", "one", "killed", "injured", "dead",
             "dies", "car", "road", "lanes", "closed", "closes", "reopens", "multi-vehicle"}


def incident_id_for(event_id):
    return "inc_" + hashlib.sha256(event_id.encode()).hexdigest()[:16]


def road_key(road):
    if not road:
        return None
    key = re.sub(r"[^A-Z0-9]", "", road.upper())
    key = re.sub(r"^INTERSTATE", "I", key)
    key = re.sub(r"^(HIGHWAY|HWY|ROUTE|RT|SR|STATEROUTE|STATEROAD)", "R", key)
    return key or None


def title_tokens(title):
    words = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", (title or "").lower())
    return {w for w in words if len(w) > 2 and w not in STOPWORDS}


def similarity(a, b):
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class IncidentEngine:
    """Correlates source events into incidents and manages incident state."""

    def __init__(self, store, cfg):
        corr = ((cfg.get("lifecycle") or {}).get("correlation") or {})
        self.store = store
        self.resolution = int(corr.get("h3_resolution", 7))
        self.rings = int(corr.get("neighbor_rings", 1))
        self.window = timedelta(minutes=int(corr.get("window_minutes", 180)))
        self.min_similarity = float(corr.get("min_title_similarity", 0.3))

    # ---- matching ----

    def match(self, con, event):
        prior = self.store.latest_version(con, event["event_id"])
        if prior and prior["incident_id"]:
            return prior["incident_id"], "same_source_event"
        refs = [f"{event['source']}:{r}" for r in event.get("references") or []]
        if event["source"] == "usgs":
            refs += [f"usgs:{i}" for i in (event["attributes"].get("ids") or []) if i]
        found = self.store.incidents_for_events(con, refs)
        if found:
            return found[0], "source_reference"
        if event.get("series_key"):
            open_series = self.store.open_series(con, event["series_key"])
            if open_series:
                return open_series["incident_id"], "same_series"
            return None, None
        if event["category"] != "roadway" or not event.get("h3"):
            return None, None
        best = None
        for cand in self._candidates(con, event):
            ok, score = self._compatible(event, cand)
            if ok and (best is None or score > best[0]):
                best = (score, cand["incident_id"])
        if best:
            return best[1], "spatiotemporal"
        return None, None

    def _candidates(self, con, event):
        cell = event["h3"]
        if geo.cell(event["lat"], event["lon"], self.resolution) != cell:
            cell = geo.cell(event["lat"], event["lon"], self.resolution)
        cells = sorted(geo.neighbors(cell, self.rings))
        t = parse_time(event["event_time"])
        return self.store.correlation_candidates(
            con, event["category"], cells, utc_iso(t - self.window), utc_iso(t + self.window)
        )

    def _compatible(self, event, cand):
        if event["type"] != cand["type"] and "closure" not in (event["type"], cand["type"]):
            return False, 0
        a, b = road_key(event.get("road")), road_key(cand.get("road"))
        if a and b and a != b:
            return False, 0
        da, db = event.get("direction"), cand.get("direction")
        if da and db and da != db:
            return False, 0
        same_road = bool(a and b and a == b)
        sim = similarity(event["title"], cand["title"])
        coarse = event["location"]["precision"] != "point" or cand["location_precision"] != "point"
        if coarse and not same_road and sim < self.min_similarity:
            return False, 0
        if not coarse and not same_road and sim < self.min_similarity / 2:
            return False, 0
        return True, sim + (1 if same_road else 0)

    # ---- state ----

    def process(self, con, event, version, content_hash, first_observed, stored_at, now):
        """Store a new source event version and update the incident it belongs to."""
        incident_id, how = self.match(con, event)
        existing = self.store.get_incident(con, incident_id) if incident_id else None
        if existing is None:
            incident_id = incident_id_for(event["event_id"])
        self.store.insert_event(con, event, content_hash, version, incident_id, first_observed, stored_at)
        reason = "source_update" if version > 1 else ("new_evidence:" + how if how else "new_evidence")
        return self.recompute(con, incident_id, existing, event["event_id"], first_observed, now, reason)

    def recompute(self, con, incident_id, existing, trigger_event_id, observed_at, now, reason):
        evidence = self.store.evidence(con, incident_id)
        events = [e["event"] for e in evidence]
        primary = min(events, key=lambda e: (RELIABILITY_RANK[e["source_reliability"]],
                                             -_ts(e.get("published_at") or e["event_time"])))
        located = min(events, key=lambda e: (LOCATION_RANK[e["location"]["confidence"]],
                                             -_ts(e.get("published_at") or e["event_time"])))
        if primary["source"] in LATEST_WINS:
            severity = primary["severity"]
        else:
            severity = max(e["severity"] for e in events)
        expiries = [e.get("expires_at") for e in evidence]
        expires_at = max((x for x in expiries if x), default=None)
        if primary["source"] in LATEST_WINS:
            expires_at = primary.get("expires_at") or expires_at
        loc = located["location"]
        geometry = located.get("geometry")
        box = geo.bbox(geometry) if geometry else None
        if box is None and located.get("lat") is not None:
            box = (located["lat"], located["lon"], located["lat"], located["lon"])
        outlets = {(e["attributes"] or {}).get("outlet") for e in events if e["source_reliability"] == "media"}
        reliabilities = {e["source_reliability"] for e in events}
        if "official" in reliabilities:
            verification = "official"
        elif "model" in reliabilities:
            verification = "model_estimate"
        elif len({o for o in outlets if o}) >= 2:
            verification = "corroborated"
        else:
            verification = "unverified"
        incident = {
            "incident_id": incident_id,
            "category": primary["category"], "type": primary["type"], "subtype": primary.get("subtype"),
            "severity": severity, "verification": verification,
            "location_confidence": loc["confidence"], "location_precision": loc.get("precision"),
            "place": loc.get("place"), "region": loc.get("region"),
            "lat": located.get("lat"), "lon": located.get("lon"), "h3": located.get("h3"),
            "min_lat": box[0] if box else None, "min_lon": box[1] if box else None,
            "max_lat": box[2] if box else None, "max_lon": box[3] if box else None,
            "geometry": json.dumps(geometry, separators=(",", ":")) if geometry else None,
            "road": next((e.get("road") for e in events if e.get("road")), None),
            "direction": next((e.get("direction") for e in events if e.get("direction")), None),
            "title": primary["title"], "description": primary.get("description"),
            "sources": ",".join(sorted({e["source"] for e in events})),
            "evidence_count": len(events),
            "event_time": min(e["event_time"] for e in events),
            "first_observed": min(x["first_observed"] for x in evidence),
            "last_updated": max(e.get("published_at") or e["event_time"] for e in events),
            "expires_at": expires_at,
            "series_key": primary.get("series_key"),
            "schema_version": SCHEMA_VERSION,
            "report_hash": existing.get("report_hash") if existing else None,
        }
        before = existing["state"] if existing else None
        if before is None:
            state, transition = "open", "first_report"
        elif before == "cleared":
            if expires_at and expires_at > now:
                state, transition = "updated", "reopened"
            else:
                state, transition = "cleared", None
        else:
            state, transition = "updated", reason
        incident["state"] = state
        incident["clear_reason"] = existing.get("clear_reason") if state == "cleared" else None
        incident["cleared_at"] = existing.get("cleared_at") if state == "cleared" else None
        self.store.save_incident(con, incident)
        if transition:
            self.store.add_history(con, incident_id, observed_at, before, state, transition, trigger_event_id)
            incident_transitions.labels(state, transition).inc()
        if state != "cleared" and expires_at and expires_at <= now:
            self.clear(con, incident, expires_at, self.clear_reason(incident))
        elif state != "cleared" and incident["category"] == "roadway":
            latest = max(events, key=lambda e: e.get("published_at") or e["event_time"])
            if latest.get("source_status") == "reopened":
                self.clear(con, incident, latest.get("published_at") or latest["event_time"], "reported_reopened")
        return incident

    def touch(self, con, incident_id, expires_at, now):
        """A re-observation with no material change: extend the incident's expiry."""
        incident = self.store.get_incident(con, incident_id)
        if incident and expires_at and (incident["expires_at"] or "") < expires_at and incident["state"] != "cleared":
            con.execute("UPDATE incidents SET expires_at = ? WHERE incident_id = ?", (expires_at, incident_id))
            return True
        return False

    @staticmethod
    def clear_reason(incident):
        sources = set((incident.get("sources") or "").split(","))
        for source in ("nws", "open_meteo"):
            if source in sources:
                return CLEAR_REASON_BY_SOURCE[source]
        return "observation_window_elapsed"

    def clear(self, con, incident, at, reason):
        con.execute("UPDATE incidents SET state = 'cleared', clear_reason = ?, cleared_at = ? WHERE incident_id = ?",
                    (reason, at, incident["incident_id"]))
        self.store.add_history(con, incident["incident_id"], at, incident["state"], "cleared", reason)
        incident_transitions.labels("cleared", reason).inc()
        incident.update(state="cleared", clear_reason=reason, cleared_at=at)

    def expire(self, con, now):
        cleared = []
        for incident in self.store.expired(con, now):
            self.clear(con, incident, incident["expires_at"], self.clear_reason(incident))
            cleared.append(incident["incident_id"])
        return cleared

    def withdraw(self, con, source, active_native_ids, at):
        """Clear open incidents the source no longer lists; reopen withdrawn ones it lists again."""
        active = {f"{source}:{i}" for i in active_native_ids}
        changed = []
        for incident in self.store.open_for_source(con, source):
            ids = {e["event_id"] for e in self.store.evidence(con, incident["incident_id"]) if e["source"] == source}
            if ids and not ids & active and (incident["expires_at"] is None or incident["expires_at"] > at):
                self.clear(con, incident, at, "source_withdrawn")
                changed.append(incident["incident_id"])
        for incident in self.store.withdrawn_for_source(con, source):
            ids = {e["event_id"] for e in self.store.evidence(con, incident["incident_id"]) if e["source"] == source}
            if ids & active and (incident["expires_at"] is None or incident["expires_at"] > at):
                con.execute("UPDATE incidents SET state = 'updated', clear_reason = NULL, cleared_at = NULL "
                            "WHERE incident_id = ?", (incident["incident_id"],))
                self.store.add_history(con, incident["incident_id"], at, "cleared", "updated", "listed_again")
                incident_transitions.labels("updated", "listed_again").inc()
                changed.append(incident["incident_id"])
        return changed


def _ts(iso):
    return parse_time(iso).timestamp()
