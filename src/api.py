import base64
import hmac
import ipaddress
import json
import os
from datetime import datetime, timezone

from flask import Response, jsonify, request

from .schema import CLEAR_REASONS, SOURCE_RELIABILITY, parse_time

MAX_LIMIT = 500
OPEN_PATHS = ("/healthz",)
DISCLAIMER = ("NWS warnings describe forecast or observed hazard areas. A warning covering a location is not "
              "evidence that the location was damaged.")


class AuthConfigError(RuntimeError):
    pass


def check_bind(host):
    """Refuse to serve beyond loopback without credentials unless explicitly allowed."""
    if os.getenv("SAURON_DASHBOARD_PASSWORD") or os.getenv("SAURON_API_TOKEN"):
        return
    if os.getenv("SAURON_ALLOW_UNAUTHENTICATED") == "1":
        return
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback:
        raise AuthConfigError(
            f"refusing to listen on {host} without SAURON_DASHBOARD_PASSWORD or SAURON_API_TOKEN; "
            "set SAURON_ALLOW_UNAUTHENTICATED=1 only when the port is bound to loopback outside this process"
        )


def install_auth(server):
    user = os.getenv("SAURON_DASHBOARD_USER", "sauron")
    password = os.getenv("SAURON_DASHBOARD_PASSWORD", "")
    token = os.getenv("SAURON_API_TOKEN", "")

    @server.before_request
    def _guard():
        if request.path in OPEN_PATHS or not (password or token):
            return None
        header = request.headers.get("Authorization", "")
        if token and header.startswith("Bearer ") and hmac.compare_digest(header[7:], token):
            return None
        if password and header.startswith("Basic "):
            try:
                given_user, _, given_pw = base64.b64decode(header[6:]).decode().partition(":")
            except ValueError:
                given_user, given_pw = "", ""
            if hmac.compare_digest(given_user, user) and hmac.compare_digest(given_pw, password):
                return None
        if request.path.startswith("/api/"):
            return _error(401, "unauthorized", "missing or invalid credentials")
        return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="SAURON"'})


def feed_health(store, now=None):
    now = now or datetime.now(timezone.utc)
    out = []
    for row in store.feed_status():
        interval = row.get("poll_interval") or 60
        if row["consecutive_failures"] >= 3:
            status = "failing"
        elif not row["last_success"]:
            status = "never_succeeded" if row["last_attempt"] else "unknown"
        elif (now - parse_time(row["last_success"])).total_seconds() > 3 * interval + 60:
            status = "stale"
        else:
            status = "ok"
        age = (now - parse_time(row["last_success"])).total_seconds() if row["last_success"] else None
        out.append({**row, "status": status, "seconds_since_success": round(age) if age is not None else None})
    return out


def _error(status, code, message):
    return jsonify({"error": {"code": code, "message": message}}), status


def _time_arg(name):
    value = request.args.get(name)
    if not value:
        return None
    try:
        return parse_time(value).isoformat()
    except ValueError:
        raise ValueError(f"{name} must be an ISO-8601 timestamp with timezone")


def _list_arg(name):
    value = request.args.get(name)
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


def _cursor(value):
    try:
        event_time, incident_id = json.loads(base64.urlsafe_b64decode(value.encode()).decode())
        return event_time, incident_id
    except Exception:
        raise ValueError("invalid cursor")


def install_api(server, store):
    @server.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    @server.get("/readyz")
    def readyz():
        try:
            counts = store.counts()
        except Exception as exc:
            return _error(503, "store_unavailable", str(exc))
        feeds = feed_health(store)
        degraded = [f["feed"] for f in feeds if f["status"] in ("failing", "stale", "never_succeeded")]
        return jsonify({"status": "degraded" if degraded else "ok", "degraded_feeds": degraded,
                        "feeds": feeds, "counts": counts}), 200

    @server.get("/api/v1/incidents")
    def incidents():
        try:
            state = request.args.get("state", "active")
            if state not in ("active", "cleared", "all"):
                raise ValueError("state must be active, cleared or all")
            location = request.args.get("location", "any")
            if location not in ("any", "mappable", "verified"):
                raise ValueError("location must be any, mappable or verified")
            limit = min(int(request.args.get("limit", 100)), MAX_LIMIT)
            if limit < 1:
                raise ValueError("limit must be positive")
            bbox = request.args.get("bbox")
            if bbox:
                bbox = [float(x) for x in bbox.split(",")]
                if len(bbox) != 4:
                    raise ValueError("bbox must be minLon,minLat,maxLon,maxLat")
            after = _cursor(request.args["cursor"]) if request.args.get("cursor") else None
            rows = store.query_incidents(
                state=state, categories=_list_arg("category"), types=_list_arg("type"),
                since=_time_arg("since"), until=_time_arg("until"), search=request.args.get("q"),
                bbox=bbox, location=location, limit=limit + 1, after=after,
            )
        except ValueError as exc:
            return _error(400, "bad_request", str(exc))
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = base64.urlsafe_b64encode(json.dumps([last["event_time"], last["incident_id"]]).encode()).decode()
        for row in rows:
            row.pop("report_hash", None)
        return jsonify({"data": rows, "next_cursor": next_cursor})

    @server.get("/api/v1/incidents/<incident_id>")
    def incident(incident_id):
        detail = store.incident_detail(incident_id)
        if not detail:
            return _error(404, "not_found", f"no incident {incident_id}")
        inc = detail["incident"]
        inc.pop("report_hash", None)
        inc["geometry"] = json.loads(inc["geometry"]) if inc.get("geometry") else None
        inc["clear_reason_text"] = CLEAR_REASONS.get(inc.get("clear_reason"))
        evidence = []
        for e in detail["evidence"]:
            ev = e["event"]
            evidence.append({
                "event_id": ev["event_id"], "version": e["version"], "source": ev["source"],
                "source_reliability": ev["source_reliability"], "title": ev["title"], "source_url": ev.get("source_url"),
                "event_time": ev["event_time"], "published_at": ev.get("published_at"),
                "first_observed": e["first_observed"], "expires_at": ev.get("expires_at"),
                "location": ev["location"], "provenance": ev.get("provenance"),
            })
        return jsonify({"incident": inc, "evidence": evidence, "history": detail["history"],
                        "source_reliability": SOURCE_RELIABILITY})

    @server.get("/api/v1/alerts/at")
    def alerts_at():
        try:
            lat, lon = float(request.args["lat"]), float(request.args["lon"])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("lat/lon out of range")
            start, end = _time_arg("start"), _time_arg("end")
            if not start or not end or start > end:
                raise ValueError("start and end are required and start must not be after end")
        except KeyError:
            return _error(400, "bad_request", "lat and lon are required")
        except ValueError as exc:
            return _error(400, "bad_request", str(exc))
        hits = store.areas_containing(lat, lon, start, end)
        return jsonify({"query": {"lat": lat, "lon": lon, "start": start, "end": end},
                        "data": hits, "disclaimer": DISCLAIMER})
