/* Demo for the SAURON project site: draws the exported offline-demo snapshot (data/demo.json).
   Map tooltips are HTML escaped by the pipeline's own tooltip_html; everything else is set as text. */
(function () {
  "use strict";

  var SEVERITY = {5: "Extreme", 4: "Severe", 3: "Moderate", 2: "Minor", 1: "Low"};
  var TILE_MESSAGE = "Basemap tiles could not be loaded. Incidents and warning areas are still drawn, without streets or labels.";
  var UNMAPPED = {
    geocoded_low: "Not drawn on the map: the place named in the text matches several places and nothing in the text picks one.",
    unresolved: "Not drawn on the map: no usable place in the text."
  };
  var state = {data: null, map: null, layers: {}, filter: "all", selected: null};

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") { node.textContent = attrs[k]; } else if (k === "class") { node.className = attrs[k]; } else { node.setAttribute(k, attrs[k]); }
    });
    (children || []).forEach(function (c) { if (c) { node.appendChild(typeof c === "string" ? document.createTextNode(c) : c); } });
    return node;
  }

  function fmt(iso) {
    return iso ? iso.slice(0, 16).replace("T", " ") + " UTC" : "";
  }

  function words(value) {
    return (value || "").replace(/_/g, " ").replace(/:/g, ": ");
  }

  function isActive(id) {
    return state.data.details[id].state !== "cleared";
  }

  function visible(id) {
    return state.filter === "all" || isActive(id);
  }

  // ---------- map ----------

  function basemaps() {
    var usgs = function (service) {
      return L.tileLayer("https://basemap.nationalmap.gov/arcgis/rest/services/" + service + "/MapServer/tile/{z}/{y}/{x}", {
        maxNativeZoom: 16, maxZoom: 19,
        attribution: 'Tiles courtesy of the <a href="https://www.usgs.gov/programs/national-geospatial-program/national-map">U.S. Geological Survey</a>'
      });
    };
    var osm = function (className) {
      return L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19, className: className || "",
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
      });
    };
    return {"USGS Topo": usgs("USGSTopo"), "USGS Imagery": usgs("USGSImageryTopo"), "OpenStreetMap": osm(), "Dark (OpenStreetMap)": osm("tiles-dark")};
  }

  function watchTiles(layer) {
    var loads = 0, errors = 0;
    layer.on("add", function () { loads = 0; errors = 0; });
    layer.on("tileload", function () { loads += 1; document.getElementById("map-status").hidden = true; });
    layer.on("tileerror", function () {
      errors += 1;
      if (!loads && errors >= 4) {
        var status = document.getElementById("map-status");
        status.textContent = TILE_MESSAGE;
        status.hidden = false;
      }
    });
  }

  function areaStyle(a, highlighted) {
    var sev = a.severity || 1;
    return {color: a.color, weight: highlighted ? 3.5 : 0.8 + sev * 0.35, opacity: a.cleared ? 0.5 : 0.95,
            fillColor: a.color, fillOpacity: a.cleared ? 0.05 : 0.06 + sev * 0.05, dashArray: a.cleared ? "4 4" : null};
  }

  function pointStyle(p, highlighted) {
    var radius = 4 + 2 * (p.severity || 1);
    var cleared = !isActive(p.id);
    if (p.verified) {
      return {radius: radius, color: highlighted ? "#ffffff" : "#0b0c10", weight: highlighted ? 3 : 1.5,
              fillColor: p.color, fillOpacity: cleared ? 0.55 : 0.95, opacity: 1};
    }
    return {radius: radius, color: p.color, weight: highlighted ? 3.5 : 2, dashArray: "3 3", fillColor: p.color, fillOpacity: 0.3, opacity: 1};
  }

  function initMap(payload) {
    var map = L.map("map", {worldCopyJump: true});
    var bases = basemaps();
    Object.keys(bases).forEach(function (k) { watchTiles(bases[k]); });
    bases["USGS Topo"].addTo(map);
    L.control.layers(bases, null, {collapsed: true, position: "topright"}).addTo(map);
    L.control.scale({imperial: true, metric: true}).addTo(map);

    payload.areas.forEach(function (a) {
      var layer = L.geoJSON(a.geometry, {style: areaStyle(a, false)});
      layer.bindTooltip(a.tooltip, {sticky: true, className: "sauron-tooltip"});
      layer.on("click", function () { select(a.id); });
      state.layers[a.id] = {layer: layer, restyle: function (on) { layer.setStyle(areaStyle(a, on)); }};
    });
    payload.points.forEach(function (p) {
      var layer = L.circleMarker([p.lat, p.lon], pointStyle(p, false));
      layer.bindTooltip(p.tooltip, {className: "sauron-tooltip", direction: "top", offset: [0, -6]});
      layer.on("click", function () { select(p.id); });
      state.layers[p.id] = {layer: layer, latlng: [p.lat, p.lon], restyle: function (on) { layer.setStyle(pointStyle(p, on)); if (on) { layer.bringToFront(); } }};
    });
    state.map = map;
    var b = payload.bounds;
    map.fitBounds(b ? [[b[0][0], b[0][1]], [b[1][0], b[1][1]]] : [[24, -125], [49.5, -66.5]], {padding: [24, 24], maxZoom: 8});
    if (window.ResizeObserver) {
      new ResizeObserver(function () { map.invalidateSize(); }).observe(document.getElementById("map"));
    }
  }

  function drawLayers() {
    Object.keys(state.layers).forEach(function (id) {
      var entry = state.layers[id];
      if (visible(id)) { entry.layer.addTo(state.map); } else { entry.layer.remove(); }
    });
  }

  function legend(payload) {
    var counts = {}, layers = {areas: 0, points: 0};
    payload.areas.forEach(function (a) { if (visible(a.id)) { counts[a.type] = (counts[a.type] || 0) + 1; layers.areas += 1; } });
    payload.points.forEach(function (p) { if (visible(p.id)) { counts[p.type] = (counts[p.type] || 0) + 1; layers.points += 1; } });
    var colors = {};
    payload.areas.concat(payload.points).forEach(function (x) { colors[x.type] = x.color; });
    var unmapped = state.data.incidents.filter(function (r) { return !r.mapped && visible(r.id); }).length;
    var summary = layers.areas + " warning areas · " + layers.points + " points";
    if (unmapped) { summary += " · " + unmapped + " without a reliable location (table only)"; }
    var box = document.getElementById("legend");
    box.replaceChildren(el("span", {class: "summary", text: summary}));
    Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a]; }).forEach(function (t) {
      var sw = el("span", {class: "swatch"});
      sw.style.background = colors[t];
      box.appendChild(el("span", {class: "item"}, [sw, words(t) + " " + counts[t]]));
    });
    box.appendChild(el("span", {class: "item"}, [el("span", {class: "key-solid"}), "verified point"]));
    box.appendChild(el("span", {class: "item"}, [el("span", {class: "key-dashed"}), "approximate (city/county)"]));
    box.appendChild(el("span", {class: "item"}, [el("span", {class: "key-area"}), "warning area"]));
  }

  // ---------- table ----------

  function renderTable() {
    var body = document.getElementById("incident-rows");
    body.replaceChildren();
    state.data.incidents.forEach(function (r) {
      if (!visible(r.id)) { return; }
      var row = el("tr", {tabindex: "0", "data-id": r.id, "aria-selected": String(r.id === state.selected)}, [
        el("td", {text: r.time}), el("td", {text: r.status}), el("td", {class: "sev-" + r.severity, text: r.sev}),
        el("td", {text: r.type}), el("td", {class: "title", text: r.title}), el("td", {text: r.location}),
        el("td", {text: r.basis}), el("td", {text: r.verification})
      ]);
      row.addEventListener("click", function () { select(r.id, true); });
      row.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(r.id, true); } });
      body.appendChild(row);
    });
  }

  // ---------- detail ----------

  function evidenceCard(e) {
    var loc = e.location || {};
    var basis = Object.keys(loc.evidence || {}).filter(function (k) { return k !== "outlet" && loc.evidence[k] !== null; })
      .map(function (k) { var v = loc.evidence[k]; return words(k) + ": " + (Array.isArray(v) ? (v.join(", ") || "none") : words(String(v))); });
    var news = e.source === "rss";
    var card = el("div", {class: "ev"}, [
      el("div", {class: "ev-head", text: e.source + " · " + e.reliability + (e.version > 1 ? " · v" + e.version : "")}),
      el("p", {text: e.title + (news ? " (fictional demo headline)" : "")}),
      el("p", {class: "kv", text: "Location: " + words(loc.confidence) + ", " + (loc.precision || "none") + (basis.length ? " (" + basis.join("; ") + ")" : "")}),
      e.severity_basis ? el("p", {class: "kv", text: "Severity basis: " + words(e.severity_basis)}) : null
    ]);
    if (e.url && !news) {
      card.appendChild(el("p", {}, [el("a", {href: e.url, rel: "noopener", target: "_blank", text: "Source record ↗"})]));
    }
    return card;
  }

  function renderDetail(id) {
    var d = state.data.details[id];
    var box = document.getElementById("detail");
    var fields = [
      ["Category / type", d.category + " / " + words(d.type) + (d.subtype ? " (" + d.subtype + ")" : "")],
      ["Event time", fmt(d.event_time)], ["Expires", fmt(d.expires_at)], ["Cleared", fmt(d.cleared_at)],
      ["Place", (d.place || "").split(";").map(function (s) { return s.trim(); }).filter(Boolean).join("; ")],
      ["Region", d.region], ["Road", d.road]
    ].filter(function (f) { return f[1]; });
    var dl = el("dl");
    fields.forEach(function (f) { dl.appendChild(el("dt", {text: f[0]})); dl.appendChild(el("dd", {text: f[1]})); });
    var children = [
      el("h3", {text: d.title}),
      el("div", {class: "badges"}, [
        el("span", {class: "badge " + d.state, text: d.state}),
        el("span", {class: "badge sev-" + d.severity, text: d.severity + " " + (SEVERITY[d.severity] || "")}),
        el("span", {class: "badge", text: words(d.verification)}),
        el("span", {class: "badge", text: d.location_basis})
      ]),
      UNMAPPED[d.location_confidence] ? el("p", {class: "callout", text: UNMAPPED[d.location_confidence]}) : null,
      d.clear_reason ? el("p", {class: "callout", text: "Clear reason: " + d.clear_reason}) : null,
      dl,
      d.description ? el("h4", {text: "Description"}) : null,
      d.description ? el("p", {class: "desc", text: d.description}) : null,
      el("h4", {text: "Evidence (" + d.evidence.length + ")"})
    ];
    d.evidence.forEach(function (e) { children.push(evidenceCard(e)); });
    children.push(el("h4", {text: "History"}));
    children.push(el("ul", {class: "history"}, d.history.map(function (h) {
      return el("li", {}, [el("time", {datetime: h.at, text: fmt(h.at)}), (h.from ? h.from + " → " : "") + h.to + " · " + words(h.reason)]);
    })));
    box.replaceChildren.apply(box, children.filter(Boolean));
    box.scrollTop = 0;
  }

  function select(id, fromTable) {
    if (state.selected && state.layers[state.selected]) { state.layers[state.selected].restyle(false); }
    state.selected = id;
    var entry = state.layers[id];
    if (entry) {
      entry.restyle(true);
      if (fromTable) {
        if (entry.latlng) { state.map.setView(entry.latlng, Math.max(state.map.getZoom(), 6)); } else { state.map.fitBounds(entry.layer.getBounds(), {padding: [24, 24], maxZoom: 8}); }
      }
    }
    document.querySelectorAll("#incident-rows tr").forEach(function (tr) { tr.setAttribute("aria-selected", String(tr.getAttribute("data-id") === id)); });
    renderDetail(id);
  }

  // ---------- filters, API examples ----------

  function setFilter(value) {
    state.filter = value;
    document.querySelectorAll(".seg").forEach(function (b) { b.setAttribute("aria-pressed", String(b.getAttribute("data-state") === value)); });
    if (state.map) { drawLayers(); }
    legend(state.data.map);
    renderTable();
  }

  function apiExamples(api) {
    var box = document.getElementById("api-examples");
    Object.keys(api).sort().forEach(function (path) {
      box.appendChild(el("details", {}, [
        el("summary", {text: "Demo response: GET " + path}),
        el("pre", {}, [el("code", {text: JSON.stringify(api[path], null, 2)})])
      ]));
    });
  }

  function summary(data) {
    var all = data.incidents.length;
    var active = data.incidents.filter(function (r) { return isActive(r.id); }).length;
    document.getElementById("count-all").textContent = "(" + all + ")";
    document.getElementById("count-active").textContent = "(" + active + ")";
    var ok = data.feeds.filter(function (f) { return f.status === "ok"; }).length;
    document.getElementById("demo-summary").textContent = data.counts.events + " source events · " + data.counts.dead_letters +
      " dead letters · " + ok + " of " + data.feeds.length + " feeds ok · as of " + fmt(data.reference_time);
  }

  function start(data) {
    state.data = data;
    summary(data);
    document.querySelectorAll(".seg").forEach(function (b) { b.addEventListener("click", function () { setFilter(b.getAttribute("data-state")); }); });
    if (window.L) {
      initMap(data.map);
    } else {
      var status = document.getElementById("map-status");
      status.textContent = "The map library did not load; the incident table below still works.";
      status.hidden = false;
    }
    setFilter("all");
    apiExamples(data.api);
    var first = data.incidents.filter(function (r) { return data.details[r.id].evidence.length > 1; })[0] || data.incidents[0];
    if (first) { select(first.id); }
  }

  function fail() {
    document.getElementById("detail").replaceChildren(el("p", {class: "callout", text: "The demo data could not be loaded. Reload the page to try again."}));
  }

  document.addEventListener("DOMContentLoaded", function () {
    fetch("data/demo.json").then(function (r) { if (!r.ok) { throw new Error(r.status); } return r.json(); }).then(start).catch(fail);
  });
})();
