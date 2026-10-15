import hashlib
import html as html_lib
import io
import json
import logging
import os
import re
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone

import dash
import dash_bootstrap_components as dbc
import pandas as pd
import plotly.express as px
from dash import ClientsideFunction, Input, Output, State, dash_table, dcc, html, no_update

from . import geo
from .api import check_bind, feed_health, install_api, install_auth
from .geocoding import STATES
from .reports import incidents_csv
from .schema import CLEAR_REASONS, parse_time

log = logging.getLogger(__name__)

FONT = 'Inter, "Segoe UI", system-ui, -apple-system, Roboto, "Helvetica Neue", Arial, sans-serif'
TYPE_COLORS = {
    "accident": "#ff3b5c", "closure": "#ff9f1a", "earthquake": "#a970ff", "flooding": "#1fb5a3",
    "fog": "#9aa4b2", "ice": "#5cc8ff", "snow": "#8fb8ff", "wind": "#2f9bff", "smoke": "#8c8c8c",
    "heavy_rain": "#3d7bfd", "thunderstorm": "#ffc53d", "tornado": "#eb2f96", "tropical": "#13c2c2",
    "dust": "#c9a227", "fire_weather": "#ff6a00", "avalanche": "#d9d9d9",
}
SEVERITY_LABELS = {5: "Extreme", 4: "Severe", 3: "Moderate", 2: "Minor", 1: "Low"}
SEVERITY_COLORS = {5: "#ff4d6d", 4: "#ff7a45", 3: "#ffc53d", 2: "#9aa4b2", 1: "#6b7280"}
LOCATION_LABELS = {
    "source_point": "Verified point", "source_area": "Warning area", "geocoded_low": "Ambiguous place",
    "unresolved": "Unresolved",
}
STATUS_COLORS = {"ok": "#52c41a", "stale": "#faad14", "failing": "#ff4d4f", "never_succeeded": "#ff4d4f", "unknown": "#8c8c8c"}
TABLE_COLUMNS = [
    ("time", "Time (UTC)", 146), ("status", "Status", 84), ("sev", "Severity", 104), ("type", "Type", 108),
    ("title", "Title", 340), ("location", "Location", 210), ("basis", "Location basis", 136),
    ("verification", "Verification", 112), ("category", "Category", 90), ("sources", "Sources", 96),
    ("updated", "Last update", 146),
]
EVENT_COLUMNS = [
    ("time", "Event (UTC)", 146), ("source", "Source", 90), ("type", "Type", 108), ("severity", "Sev", 60),
    ("location", "Location basis", 136), ("published", "Published", 146), ("observed", "First seen", 146),
    ("incident_id", "Incident", 170), ("title", "Title", 420),
]
ATTRIBUTION = ("Basemaps: U.S. Geological Survey The National Map; © OpenStreetMap contributors. Data: National "
               "Weather Service, USGS, DataSF, Open-Meteo (CC BY 4.0, non-commercial tier), Google News headlines (unverified).")
CONUS = (24.0, -125.0, 49.5, -66.5)


class TTLCache:
    def __init__(self, ttl):
        self.ttl = ttl
        self.data = {}
        self.lock = threading.Lock()

    def get(self, key, compute):
        with self.lock:
            hit = self.data.get(key)
            if hit and time.monotonic() - hit[0] < self.ttl:
                return hit[1]
        value = compute()
        with self.lock:
            self.data[key] = (time.monotonic(), value)
            if len(self.data) > 256:
                self.data.pop(next(iter(self.data)))
        return value


# ---------- formatting ----------

def fmt_time(iso):
    if not iso:
        return ""
    try:
        return parse_time(iso).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def date_bounds(start_date, end_date):
    since = f"{start_date}T00:00:00+00:00" if start_date else None
    until = f"{end_date}T23:59:59+00:00" if end_date else None
    return since, until


def default_dates(reference=None):
    today = datetime.fromisoformat(reference).date() if reference else datetime.now(timezone.utc).date()
    return (today - timedelta(days=3)).isoformat(), (today + timedelta(days=2)).isoformat()


def place_parts(place):
    return [p.strip() for p in re.split(r";", place or "") if p.strip()]


def location_basis(row):
    conf = row.get("location_confidence")
    if conf in ("geocoded_high", "geocoded_medium"):
        return f"Approx. {row.get('location_precision') or 'place'}"
    if conf == "unresolved" and row.get("location_precision") == "state":
        return "State only"
    return LOCATION_LABELS.get(conf, conf or "")


def location_summary(row):
    parts = place_parts(row.get("place"))
    region = ", ".join(r for r in (row.get("region") or "").split(",") if r)
    head = parts[0] if parts else ""
    if len(parts) > 1:
        head = f"{head} +{len(parts) - 1} more"
    if region and region not in head:
        head = f"{head} · {region}" if head else region
    return head or "Unresolved"


def severity_label(sev):
    return f"{sev} {SEVERITY_LABELS.get(sev, '')}".strip()


def table_rows(rows):
    out, tips = [], []
    for r in rows:
        out.append({
            "id": r["incident_id"], "time": fmt_time(r["event_time"]), "status": r["state"].title(),
            "sev": severity_label(r["severity"]), "severity": r["severity"], "category": r["category"],
            "type": r["type"].replace("_", " "), "title": r["title"], "location": location_summary(r),
            "basis": location_basis(r), "verification": r["verification"].replace("_", " "),
            "sources": (r.get("sources") or "").replace(",", ", "), "updated": fmt_time(r.get("last_updated")),
        })
        tips.append({"title": {"value": r["title"], "type": "text"},
                     "location": {"value": "; ".join(place_parts(r.get("place"))) or "Unresolved", "type": "text"}})
    return out, tips


def event_rows(rows):
    return [{
        "id": f"{r['event_id']}#{r['version']}", "time": fmt_time(r["event_time"]), "source": r["source"],
        "type": r["type"].replace("_", " "), "severity": r["severity"],
        "location": LOCATION_LABELS.get(r["location_confidence"], f"Approx. ({r['location_confidence']})")
        if r["location_confidence"] not in ("geocoded_high", "geocoded_medium") else "Approx. place",
        "published": fmt_time(r.get("published_at")), "observed": fmt_time(r.get("first_observed")),
        "incident_id": r.get("incident_id") or "", "title": r["title"],
    } for r in rows]


# ---------- map ----------

_SIMPLIFIED = {}
_SIMPLIFIED_LOCK = threading.Lock()


def display_geometry(row):
    """Polygon simplified for drawing (about 1 km); containment queries use the stored geometry."""
    key = (row["incident_id"], hashlib.sha1(row["geometry"].encode()).hexdigest())
    with _SIMPLIFIED_LOCK:
        if key in _SIMPLIFIED:
            return _SIMPLIFIED[key]
    shape = geo.simplify(json.loads(row["geometry"]), 0.01)
    shape = geo.round_geometry(shape, 4) if shape else None
    with _SIMPLIFIED_LOCK:
        if len(_SIMPLIFIED) > 4000:
            _SIMPLIFIED.clear()
        _SIMPLIFIED[key] = shape
    return shape


def tooltip_html(row):
    esc = html_lib.escape
    lines = [
        f"<b>{esc(row['title'])}</b>",
        f"{esc(row['type'].replace('_', ' ').title())} · {esc(severity_label(row['severity']))} · {esc(row['state'])}",
        f"{esc(row['verification'].replace('_', ' '))} · location: {esc(location_basis(row))}",
        esc(location_summary(row)),
        f"Sources: {esc((row.get('sources') or '').replace(',', ', '))} · event {esc(fmt_time(row['event_time']))} UTC",
    ]
    if row.get("expires_at") and row["state"] != "cleared":
        lines.append(f"Expires {esc(fmt_time(row['expires_at']))} UTC")
    if row["state"] == "cleared" and row.get("clear_reason"):
        lines.append(esc(CLEAR_REASONS.get(row["clear_reason"], row["clear_reason"])))
    return "<br>".join(lines)


def _inside(box, outer):
    return box[0] >= outer[0] and box[1] >= outer[1] and box[2] <= outer[2] and box[3] <= outer[3]


def map_bounds(boxes):
    """Fit to the contiguous US when most features are there, so one Guam or Aleutian alert
    does not zoom the map out to the whole Pacific."""
    if not boxes:
        return None
    inside = [b for b in boxes if _inside(b, CONUS)]
    use = inside if len(inside) * 2 >= len(boxes) else boxes
    return [[min(b[0] for b in use), min(b[1] for b in use)], [max(b[2] for b in use), max(b[3] for b in use)]]


def map_payload(rows, fit_key):
    areas, points, boxes, sig = [], [], [], []
    for r in rows:
        color = TYPE_COLORS.get(r["type"], "#d9d9d9")
        if r["location_confidence"] == "source_area" and r.get("geometry"):
            shape = display_geometry(r)
            if not shape:
                continue
            areas.append({"id": r["incident_id"], "type": r["type"], "severity": r["severity"], "color": color,
                          "cleared": r["state"] == "cleared", "tooltip": tooltip_html(r), "geometry": shape})
            boxes.append((r["min_lat"], r["min_lon"], r["max_lat"], r["max_lon"]))
        elif r.get("lat") is not None and r.get("lon") is not None:
            layer = {"seismic": "seismic", "weather": "weather"}.get(r["category"], "roadway")
            points.append({"id": r["incident_id"], "lat": r["lat"], "lon": r["lon"], "layer": layer,
                           "type": r["type"], "severity": r["severity"], "color": color,
                           "verified": r["location_confidence"] == "source_point", "tooltip": tooltip_html(r)})
            boxes.append((r["lat"], r["lon"], r["lat"], r["lon"]))
        else:
            continue
        sig.append((r["incident_id"], r["state"], r["severity"], r["last_updated"]))
    signature = hashlib.sha1(json.dumps(sig).encode()).hexdigest()
    counts = {}
    for item in areas + points:
        counts[item["type"]] = counts.get(item["type"], 0) + 1
    layers = {"areas": len(areas), **{k: sum(1 for p in points if p["layer"] == k) for k in ("roadway", "weather", "seismic")}}
    return {"areas": areas, "points": points, "bounds": map_bounds(boxes), "fit_key": fit_key,
            "signature": f"{signature}:{fit_key}", "counts": counts, "layers": layers,
            "unmapped": sum(1 for r in rows if r["location_confidence"] in ("geocoded_low", "unresolved")
                            or (r["location_confidence"] == "source_area" and not r.get("geometry")))}


def map_legend(payload):
    if not payload:
        return []
    swatches = [html.Span([html.Span(className="swatch", style={"background": TYPE_COLORS.get(t, "#d9d9d9")}),
                           f"{t.replace('_', ' ')} {n}"], className="legend-item")
                for t, n in sorted(payload["counts"].items(), key=lambda kv: -kv[1])]
    layers = payload["layers"]
    summary = (f"{layers['areas']} warning areas · {layers['roadway']} roadway · {layers['weather']} weather points · "
               f"{layers['seismic']} earthquakes")
    if payload.get("unmapped"):
        summary += f" · {payload['unmapped']} without a reliable location (table only)"
    return [html.Div(summary, className="legend-summary"),
            html.Div(swatches + [
                html.Span([html.Span(className="key-solid"), "verified point"], className="legend-item"),
                html.Span([html.Span(className="key-dashed"), "approximate (city/county)"], className="legend-item"),
                html.Span([html.Span(className="key-area"), "warning area"], className="legend-item"),
            ], className="legend-items")]


# ---------- details ----------

def _badge(text, color=None):
    if color is None:
        return html.Span(text, className="badge-chip neutral")
    return html.Span(text, className="badge-chip", style={"borderColor": color, "color": color})


def _field(label, value):
    return [html.Dt(label), html.Dd(value if value not in (None, "") else "—")]


def detail_panel(detail):
    if not detail:
        return html.Div([html.Div("No incident selected", className="detail-empty-title"),
                         html.Div("Select a row in the table, or click an area or marker on the map.",
                                  className="detail-empty-text")], className="detail-empty")
    inc = detail["incident"]
    sev = inc["severity"]
    state_color = {"open": "#52c41a", "updated": "#2f9bff", "cleared": "#9aa4b2"}.get(inc["state"], "#9aa4b2")
    badges = html.Div([
        _badge(inc["state"].title(), state_color),
        _badge(severity_label(sev), SEVERITY_COLORS.get(sev, "#9aa4b2")),
        _badge(inc["verification"].replace("_", " ")),
        _badge(location_basis(inc)),
    ], className="detail-badges")
    places = place_parts(inc.get("place"))
    coords = f"{inc['lat']:.4f}, {inc['lon']:.4f}" if inc.get("lat") is not None else None
    if inc["state"] == "cleared":
        cleared = f"{fmt_time(inc['cleared_at'])} UTC · {CLEAR_REASONS.get(inc['clear_reason'], inc['clear_reason'])}"
    else:
        cleared = None
    facts = html.Dl(
        _field("Category / type", f"{inc['category']} / {inc['type'].replace('_', ' ')}"
               + (f" ({inc['subtype']})" if inc.get("subtype") else ""))
        + _field("Event time", f"{fmt_time(inc['event_time'])} UTC")
        + _field("First seen", f"{fmt_time(inc['first_observed'])} UTC")
        + _field("Last source update", f"{fmt_time(inc['last_updated'])} UTC")
        + _field("Expires", f"{fmt_time(inc['expires_at'])} UTC" if inc.get("expires_at") else None)
        + _field("Cleared", cleared)
        + _field("Region", ", ".join(r for r in (inc.get("region") or "").split(",") if r))
        + _field("Coordinates", f"{coords} ({inc['location_precision']})" if coords else None)
        + _field("Road", f"{inc['road']}" + (f" ({inc['direction']})" if inc.get("direction") else "") if inc.get("road") else None)
        + _field("Sources", f"{(inc.get('sources') or '').replace(',', ', ')} · {inc['evidence_count']} record(s)"),
        className="detail-facts")
    sections = [html.Div(inc["title"], className="detail-title"), badges, facts]
    if places:
        sections += [html.Div(f"Affected area ({len(places)})" if len(places) > 1 else "Place", className="detail-heading"),
                     html.Div("; ".join(places), className="detail-scroll detail-places")]
    if inc.get("description"):
        sections += [html.Div("Description", className="detail-heading"),
                     html.Div(inc["description"], className="detail-scroll detail-description")]
    evidence = []
    for e in detail["evidence"]:
        ev = e["event"]
        basis = ev["location"].get("evidence") or {}
        evidence.append(html.Li([
            html.Div([html.Span(f"{ev['source']} · {ev['source_reliability']}", className="ev-source"),
                      html.A("source", href=ev["source_url"], target="_blank", rel="noopener noreferrer",
                             className="ev-link") if ev.get("source_url") else None]),
            html.Div(ev["title"], className="ev-title"),
            html.Div(f"event {fmt_time(ev['event_time'])} · published {fmt_time(ev.get('published_at')) or 'n/a'} · "
                     f"seen {fmt_time(e['first_observed'])} · v{e['version']}", className="ev-meta"),
            html.Div("location basis: " + ", ".join(f"{k}={v}" for k, v in sorted(basis.items()) if v not in (None, "", [])),
                     className="ev-meta") if basis else None,
        ]))
    sections += [html.Div(f"Evidence ({len(evidence)})", className="detail-heading"), html.Ul(evidence, className="detail-evidence")]
    history = [html.Li([html.Span(f"{fmt_time(h['at'])}", className="hist-time"),
                        f" {h['from_state'] or '—'} → {h['to_state']} ", html.Span(h["reason"], className="hist-reason")])
               for h in detail["history"]]
    sections += [html.Div("History", className="detail-heading"), html.Ul(history, className="detail-history"),
                 html.Div(inc["incident_id"], className="detail-id")]
    return html.Div(sections, className="detail-card")


# ---------- tables ----------

def table_styles(dark):
    bg, alt, fg, muted, line, head = (("#11141a", "#141821", "#e6e9ef", "#9aa4b2", "rgba(255,255,255,.06)", "#0c0e13")
                                      if dark else ("#ffffff", "#f6f7f9", "#1f2329", "#5f6b7a", "#e6e8ec", "#eef0f3"))
    return {
        "style_cell": {"fontFamily": FONT, "fontSize": "13px", "padding": "0 10px", "height": "34px",
                       "whiteSpace": "nowrap", "overflow": "hidden", "textOverflow": "ellipsis", "textAlign": "left",
                       "border": "0", "borderBottom": f"1px solid {line}", "backgroundColor": bg, "color": fg},
        "style_header": {"backgroundColor": head, "color": muted, "fontWeight": "600", "fontSize": "11px",
                         "textTransform": "uppercase", "letterSpacing": ".06em", "borderBottom": f"1px solid {line}",
                         "height": "36px"},
        "style_filter": {"backgroundColor": bg, "color": fg, "borderBottom": f"1px solid {line}"},
        "alt": alt,
    }


def column_specs(columns):
    return ([{"name": name, "id": cid} for cid, name, _ in columns],
            [{"if": {"column_id": cid}, "width": f"{w}px", "minWidth": f"{w}px", "maxWidth": f"{w}px"}
             for cid, _, w in columns])


def incident_conditionals(dark, selected=None):
    styles = table_styles(dark)
    active_bg, selected_bg = ("#2a1820", "#37171f") if dark else ("#fff0f3", "#ffdce3")
    rules = [{"if": {"row_index": "odd"}, "backgroundColor": styles["alt"]}]
    for sev, color in SEVERITY_COLORS.items():
        rules.append({"if": {"filter_query": f"{{severity}} = {sev}", "column_id": "sev"}, "color": color, "fontWeight": "600"})
    rules += [
        {"if": {"filter_query": '{status} = "Cleared"'}, "color": "#7d8590"},
        {"if": {"filter_query": '{verification} = "unverified"', "column_id": "verification"}, "color": "#ffc53d"},
        {"if": {"filter_query": '{basis} = "Unresolved" || {basis} = "Ambiguous place"', "column_id": "basis"},
         "color": "#7d8590", "fontStyle": "italic"},
        {"if": {"state": "active"}, "backgroundColor": active_bg, "border": "0",
         "borderBottom": "1px solid #ff254e"},
    ]
    if selected:
        rules.append({"if": {"filter_query": f'{{id}} = "{selected}"'}, "backgroundColor": selected_bg})
        rules.append({"if": {"filter_query": f'{{id}} = "{selected}"', "column_id": "time"},
                      "boxShadow": "inset 3px 0 0 #ff254e"})
    return rules


def analytics_figures(rows, events, theme):
    tpl = "plotly_dark" if theme == "dark" else "plotly_white"
    df = pd.DataFrame(rows)
    figs = []
    if df.empty:
        empty = pd.DataFrame({"x": [], "y": []})
        return [px.bar(empty, x="x", y="y", title=t, template=tpl)
                for t in ("Incidents by type", "Source events per hour", "Verification", "Location confidence")]
    by_type = df.groupby(["type", "category"]).size().reset_index(name="count")
    figs.append(px.bar(by_type, x="type", y="count", color="category", title="Incidents by type", template=tpl))
    ev = pd.DataFrame(events)
    if ev.empty:
        figs.append(px.line(pd.DataFrame({"hour": [], "count": []}), x="hour", y="count", title="Source events per hour", template=tpl))
    else:
        ev["hour"] = pd.to_datetime(ev["event_time"], utc=True).dt.floor("h")
        per = ev.groupby(["hour", "source"]).size().reset_index(name="count")
        figs.append(px.line(per, x="hour", y="count", color="source", title="Source events per hour", template=tpl))
    for col, title in (("verification", "Verification"), ("location_confidence", "Location confidence")):
        counts = df[col].value_counts().reset_index()
        counts.columns = [col, "count"]
        figs.append(px.bar(counts, x=col, y="count", title=title, template=tpl))
    for f in figs:
        f.update_layout(uirevision="analytics", margin=dict(l=30, r=10, t=40, b=30),
                        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font=dict(family=FONT))
    return figs


# ---------- app ----------

def create_app(store, cfg):
    reports_dir = os.path.join((cfg.get("reports") or {}).get("dir", "reports"), "incidents")
    app = dash.Dash(__name__, title="SAURON", assets_folder=os.path.join(os.path.dirname(__file__), "assets"),
                    suppress_callback_exceptions=False)
    install_auth(app.server)
    install_api(app.server, store)
    cache = TTLCache(5)
    start_default, end_default = default_dates((cfg.get("dashboard") or {}).get("reference_date"))
    dark = table_styles(True)
    inc_columns, inc_widths = column_specs(TABLE_COLUMNS)
    ev_columns, ev_widths = column_specs(EVENT_COLUMNS)

    def incidents_for(f, types=None, geometry=False, location=None):
        since, until = date_bounds(f["start"], f["end"])
        loc = location or f["location"]
        key = ("inc", f["state"], tuple(f["categories"] or ()), loc, f["search"] or "", since, until,
               tuple(f["regions"] or ()), f["min_severity"], tuple(types or ()), geometry)
        return cache.get(key, lambda: store.query_incidents(
            state=f["state"], categories=f["categories"], types=types, since=since, until=until, search=f["search"],
            location=loc, limit=3000, geometry=geometry, regions=f["regions"], min_severity=f["min_severity"],
            overlap=True))

    header = html.Header(className="topbar", children=[
        html.Div([html.Img(src=app.get_asset_url("logo.svg"), className="brand-eye", alt=""),
                  html.Span("SAURON", className="brand-text"),
                  html.Span("Geospatial hazard intelligence", className="brand-sub")], className="brand-wrap"),
        html.Div(id="kpis", className="kpis"),
        html.Div([
            dcc.Dropdown(id="refresh", value=30000, clearable=False, searchable=False, className="compact-select",
                         options=[{"label": "Refresh 15 s", "value": 15000}, {"label": "Refresh 30 s", "value": 30000},
                                  {"label": "Refresh 60 s", "value": 60000}, {"label": "Paused", "value": 0}]),
            dbc.Switch(id="dark_mode", value=True, label="Dark", className="theme-switch"),
            html.Div(id="status_dot", className="status-dot", title="feed health"),
        ], className="topbar-actions"),
    ])

    def labeled(label, control, width):
        return html.Div([html.Label(label, className="ctl-label"), control], className=f"filter {width}")

    filters = html.Div(className="filters panel", children=[
        labeled("Search", dcc.Input(id="search", type="text", placeholder="Title, place, road, source", debounce=True,
                                    className="form-control ctl-input"), "w-search"),
        labeled("Active between (UTC)", dcc.DatePickerRange(id="date_range", start_date=start_default,
                                                           end_date=end_default, minimum_nights=0, clearable=True,
                                                           display_format="MMM D, YYYY", className="ctl-dates"), "w-dates"),
        labeled("Status", dcc.Dropdown(id="state", value="active", clearable=False, searchable=False, options=[
            {"label": "Active", "value": "active"}, {"label": "Cleared (history)", "value": "cleared"},
            {"label": "All", "value": "all"}]), "w-sm"),
        labeled("Categories", dcc.Dropdown(id="categories", multi=True, value=["roadway", "weather", "seismic"],
                                           placeholder="All categories",
                                           options=[{"label": c.title(), "value": c} for c in ("roadway", "weather", "seismic")]),
                "w-cat"),
        labeled("US states", dcc.Dropdown(id="regions", multi=True, placeholder="All states",
                                          options=[{"label": f"{code} · {name}", "value": code}
                                                   for code, name in sorted(STATES.items())]), "w-md"),
        labeled("Min severity", dcc.Dropdown(id="min_severity", value=None, placeholder="Any", searchable=False,
                                             options=[{"label": severity_label(s), "value": s} for s in (2, 3, 4, 5)]),
                "w-sm"),
        labeled("Locations", dcc.Dropdown(id="location", value="any", clearable=False, searchable=False, options=[
            {"label": "Any", "value": "any"}, {"label": "Mappable", "value": "mappable"},
            {"label": "Verified only", "value": "verified"}]), "w-sm"),
    ])

    incident_table = dash_table.DataTable(
        id="incident_table", columns=inc_columns, data=[], tooltip_data=[], tooltip_duration=None,
        sort_action="native", sort_by=[{"column_id": "time", "direction": "desc"}], filter_action="native",
        filter_options={"case": "insensitive", "placeholder_text": "filter…"},
        page_action="native", page_size=100, fixed_rows={"headers": True}, cell_selectable=True,
        style_table={"height": "calc(100vh - 300px)", "minHeight": "360px", "overflowY": "auto", "overflowX": "auto"},
        style_cell=dark["style_cell"], style_header=dark["style_header"], style_filter=dark["style_filter"],
        style_cell_conditional=inc_widths, style_data_conditional=incident_conditionals(True),
        css=[{"selector": ".dash-spreadsheet td div", "rule": "line-height: 34px; max-height: 34px; overflow: hidden;"},
             {"selector": ".dash-table-tooltip", "rule": "max-width: 520px; white-space: normal;"}],
    )
    event_table = dash_table.DataTable(
        id="event_table", columns=ev_columns, data=[], sort_action="native", filter_action="native",
        filter_options={"case": "insensitive", "placeholder_text": "filter…"},
        page_action="native", page_size=100, fixed_rows={"headers": True},
        style_table={"height": "calc(100vh - 318px)", "minHeight": "360px", "overflowY": "auto", "overflowX": "auto"},
        style_cell=dark["style_cell"], style_header=dark["style_header"], style_filter=dark["style_filter"],
        style_cell_conditional=ev_widths, style_data_conditional=[{"if": {"row_index": "odd"}, "backgroundColor": dark["alt"]}],
    )

    map_tab = html.Div(className="map-layout", children=[
        html.Div(className="map-main panel", children=[
            html.Div(className="map-toolbar", children=[
                dcc.RadioItems(id="map_mode", value="dots", inline=True, className="mode-radio", options=[
                    {"label": "Markers", "value": "dots"}, {"label": "Cluster", "value": "cluster"},
                    {"label": "Heatmap", "value": "heat"}]),
                dcc.Dropdown(id="type_filter", multi=True, placeholder="All hazard types", className="type-filter",
                             options=[{"label": k.replace("_", " ").title(), "value": k} for k in TYPE_COLORS]),
            ]),
            html.Div(className="map-frame", children=[
                html.Div(id="incident_map", className="incident-map"),
                html.Div(id="map_status", className="map-status hidden"),
            ]),
            html.Div(id="map_legend", className="map-legend"),
        ]),
        html.Div(id="map_detail", className="detail-panel panel"),
    ])

    app.layout = html.Div(id="root", className="theme-dark", children=[
        dcc.Store(id="selected_incident"),
        dcc.Store(id="map_data"),
        dcc.Store(id="map_signature"),
        dcc.Store(id="map_resize"),
        header, filters,
        dbc.Tabs(id="tabs", active_tab="tab-incidents", className="tabs-neo", children=[
            dbc.Tab(label="Incidents", tab_id="tab-incidents", children=[html.Div(className="incident-layout", children=[
                html.Div(dcc.Loading(incident_table, type="dot", delay_show=600, parent_className="loading-local"),
                         className="table-wrap panel"),
                html.Div(id="detail", className="detail-panel panel"),
            ])]),
            dbc.Tab(label="Map", tab_id="tab-map", children=[map_tab]),
            dbc.Tab(label="Source events", tab_id="tab-events", children=[html.Div(event_table, className="table-wrap panel")]),
            dbc.Tab(label="Analytics", tab_id="tab-analytics", children=[html.Div(className="analytics-grid panel", children=[
                dcc.Graph(id="by_type"), dcc.Graph(id="over_time"), dcc.Graph(id="by_verification"), dcc.Graph(id="by_location"),
            ])]),
            dbc.Tab(label="System health", tab_id="tab-health", children=[html.Div(id="health", className="panel")]),
        ]),
        html.Footer(className="footer", children=[
            html.Div([html.Button("Export CSV", id="export_csv", className="btn btn-neo btn-sm me-2"),
                      html.Button("Download reports ZIP", id="btn_reports", className="btn btn-neo btn-sm")]),
            html.Div(ATTRIBUTION, className="attribution"),
            dcc.Download(id="download_csv"), dcc.Download(id="download_reports"),
        ]),
        dcc.Interval(id="tick", interval=30000, n_intervals=0),
    ])

    filter_inputs = [Input("search", "value"), Input("date_range", "start_date"), Input("date_range", "end_date"),
                     Input("state", "value"), Input("categories", "value"), Input("regions", "value"),
                     Input("min_severity", "value"), Input("location", "value")]

    def collect(search, start, end, state, categories, regions, min_severity, location):
        return {"search": search, "start": start, "end": end, "state": state, "categories": categories or None,
                "regions": regions or None, "min_severity": min_severity, "location": location}

    @app.callback(Output("root", "className"),
                  Output("incident_table", "style_cell"), Output("incident_table", "style_header"),
                  Output("incident_table", "style_filter"),
                  Output("event_table", "style_cell"), Output("event_table", "style_header"),
                  Output("event_table", "style_filter"), Output("event_table", "style_data_conditional"),
                  Input("dark_mode", "value"))
    def _theme(is_dark):
        s = table_styles(bool(is_dark))
        odd = [{"if": {"row_index": "odd"}, "backgroundColor": s["alt"]}]
        return ("theme-dark" if is_dark else "theme-light", s["style_cell"], s["style_header"], s["style_filter"],
                s["style_cell"], s["style_header"], s["style_filter"], odd)

    @app.callback(Output("tick", "interval"), Output("tick", "disabled"), Input("refresh", "value"))
    def _interval(ms):
        return (ms or 30000), not ms

    @app.callback(Output("incident_table", "data"), Output("incident_table", "tooltip_data"), Output("kpis", "children"),
                  Output("by_type", "figure"), Output("over_time", "figure"),
                  Output("by_verification", "figure"), Output("by_location", "figure"),
                  Input("tick", "n_intervals"), *filter_inputs, Input("dark_mode", "value"))
    def _incidents(_n, *args):
        *values, is_dark = args
        f = collect(*values)
        rows = incidents_for(f)
        since, until = date_bounds(f["start"], f["end"])
        events = cache.get(("ev", since, until, f["search"] or ""),
                           lambda: store.query_events(since=since, until=until, search=f["search"], limit=5000))
        active = cache.get(("kpi",), lambda: store.read(
            "SELECT category, COUNT(*) n FROM incidents WHERE state != 'cleared' GROUP BY category"))
        counts = {r["category"]: r["n"] for r in active}
        kpis = [html.Span([html.B(counts.get(c, 0)), f" active {c}"], className="kpi") for c in ("roadway", "weather", "seismic")]
        data, tips = table_rows(rows)
        return (data, tips, kpis, *analytics_figures(rows, events, "dark" if is_dark else "light"))

    @app.callback(Output("map_data", "data"), Output("map_signature", "data"), Output("map_legend", "children"),
                  Input("tick", "n_intervals"), *filter_inputs, Input("type_filter", "value"),
                  State("map_signature", "data"))
    def _map_data(_n, *args):
        *values, types, last_signature = args
        f = collect(*values)
        location = "mappable" if f["location"] == "any" else f["location"]
        rows = incidents_for(f, types=types or None, geometry=True)
        all_rows = incidents_for(f, types=types or None)
        fit_key = hashlib.sha1(json.dumps([values, types], default=str).encode()).hexdigest()[:12]
        payload = map_payload([r for r in rows if location != "verified" or
                               r["location_confidence"] in ("source_point", "source_area")], fit_key)
        payload["unmapped"] = sum(1 for r in all_rows if r["location_confidence"] in ("geocoded_low", "unresolved"))
        if payload["signature"] == last_signature:
            return no_update, no_update, map_legend(payload)
        return payload, payload["signature"], map_legend(payload)

    app.clientside_callback(
        ClientsideFunction(namespace="sauron", function_name="renderMap"),
        Output("map_status", "children"), Output("map_status", "className"),
        Input("map_data", "data"), Input("map_mode", "value"), Input("selected_incident", "data"),
    )
    app.clientside_callback(
        ClientsideFunction(namespace="sauron", function_name="resizeMap"),
        Output("map_resize", "data"), Input("tabs", "active_tab"),
    )

    @app.callback(Output("selected_incident", "data"), Input("incident_table", "active_cell"),
                  prevent_initial_call=True)
    def _select_from_table(cell):
        if not cell or not cell.get("row_id"):
            return no_update
        return cell["row_id"]

    @app.callback(Output("detail", "children"), Output("map_detail", "children"),
                  Output("incident_table", "style_data_conditional"),
                  Input("selected_incident", "data"), Input("dark_mode", "value"))
    def _detail(selected, is_dark):
        panel = detail_panel(store.incident_detail(selected) if selected else None)
        return panel, panel, incident_conditionals(bool(is_dark), selected)

    @app.callback(Output("event_table", "data"), Input("tick", "n_intervals"), Input("search", "value"),
                  Input("date_range", "start_date"), Input("date_range", "end_date"))
    def _events(_n, search, start, end):
        since, until = date_bounds(start, end)
        return event_rows(store.query_events(since=since, until=until, search=search, limit=1000))

    @app.callback(Output("health", "children"), Output("status_dot", "style"), Input("tick", "n_intervals"),
                  Input("dark_mode", "value"))
    def _health(_n, is_dark):
        feeds = feed_health(store)
        counts = store.counts()
        worst = "unknown"
        if feeds:
            order = ["failing", "never_succeeded", "stale", "ok"]
            worst = next(s for s in order if any(f["status"] == s for f in feeds))
        s = table_styles(bool(is_dark))
        rows = [{**f, "last_success": fmt_time(f["last_success"]), "last_error_at": fmt_time(f["last_error_at"])} for f in feeds]
        feed_table = dash_table.DataTable(
            columns=[{"name": c.replace("_", " ").title(), "id": c} for c in
                     ("feed", "kind", "status", "seconds_since_success", "last_success", "items_last_poll",
                      "published_last_poll", "consecutive_failures", "last_error_category", "last_error_at")],
            data=rows, style_cell=s["style_cell"], style_header=s["style_header"],
            style_data_conditional=[{"if": {"filter_query": f"{{status}} = {k}", "column_id": "status"}, "color": c}
                                    for k, c in STATUS_COLORS.items()],
        )
        dead = store.dead_letter_sample()
        body = [html.H6("Feeds"), html.Div("A healthy feed with zero items is normal: it polled successfully and the "
                                           "source had nothing new.", className="muted mb-2"), feed_table,
                html.H6("Store", className="mt-3"),
                html.Div(f"{counts['events']} source events ({counts['event_versions']} versions) · "
                         f"{counts['dead_letters']} dead letters"),
                html.Ul([html.Li(f"{r['state']} {r['category']}: {r['n']}") for r in counts["incidents"]])]
        if dead:
            body += [html.H6("Recent dead letters", className="mt-3"),
                     html.Ul([html.Li(f"{d['received_at']} [{d['source']}] {d['reason']} ({d['location'] or '-'})") for d in dead])]
        dot = {"background": STATUS_COLORS[worst], "boxShadow": f"0 0 10px {STATUS_COLORS[worst]}"}
        return body, dot

    @app.callback(Output("download_csv", "data"), Input("export_csv", "n_clicks"),
                  State("search", "value"), State("date_range", "start_date"), State("date_range", "end_date"),
                  State("state", "value"), State("categories", "value"), State("regions", "value"),
                  State("min_severity", "value"), State("location", "value"), prevent_initial_call=True)
    def _export(_n, *values):
        return dict(content=incidents_csv(incidents_for(collect(*values))), filename="incidents.csv", type="text/csv")

    @app.callback(Output("download_reports", "data"), Input("btn_reports", "n_clicks"), prevent_initial_call=True)
    def _reports(_n):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            if os.path.isdir(reports_dir):
                for name in sorted(os.listdir(reports_dir)):
                    if name.endswith(".txt"):
                        z.write(os.path.join(reports_dir, name), arcname=name)
        return dcc.send_bytes(buf.getvalue(), "incident-reports.zip")

    return app


def run_dashboard(store, cfg):
    app_cfg = cfg.get("app") or {}
    host = app_cfg.get("host", "127.0.0.1")
    check_bind(host)
    app = create_app(store, cfg)
    app.run(debug=bool(app_cfg.get("debug")), host=host, port=int(app_cfg.get("port", 8050)))
