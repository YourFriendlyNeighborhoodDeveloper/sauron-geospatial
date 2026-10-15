/* Incident map for the SAURON dashboard (Leaflet, no WebGL required).
   Tooltip HTML is built and escaped on the server. */
(function () {
  "use strict";

  var state = null;
  var US_VIEW = [[24.0, -125.0], [49.5, -66.5]];
  var ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
  var TILE_MESSAGE = "Basemap tiles could not be loaded (offline, or blocked by the network). Incidents and warning areas are still drawn, without streets or labels.";

  function status(message, level) {
    return [message || "", "map-status " + (message ? level : "hidden")];
  }

  function setStatus(message, level) {
    if (window.dash_clientside && window.dash_clientside.set_props) {
      window.dash_clientside.set_props("map_status", {children: message || "", className: "map-status " + (message ? level || "info" : "hidden")});
    }
  }

  function basemaps() {
    var usgs = function (service) {
      return L.tileLayer("https://basemap.nationalmap.gov/arcgis/rest/services/" + service + "/MapServer/tile/{z}/{y}/{x}", {
        maxNativeZoom: 16, maxZoom: 19,
        attribution: 'Tiles courtesy of the <a href="https://www.usgs.gov/programs/national-geospatial-program/national-map">U.S. Geological Survey</a>'
      });
    };
    var osm = function (className) {
      return L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {maxZoom: 19, attribution: ATTRIBUTION, className: className || ""});
    };
    return {
      "USGS Topo": usgs("USGSTopo"),
      "USGS Imagery": usgs("USGSImageryTopo"),
      "OpenStreetMap": osm(),
      "Dark (OpenStreetMap)": osm("tiles-dark")
    };
  }

  function watchTiles(layer) {
    layer.on("tileload", function () {
      state.tileLoads += 1;
      if (state.tileWarning) { state.tileWarning = false; setStatus(state.overlayMessage, "info"); }
    });
    layer.on("tileerror", function () {
      state.tileErrors += 1;
      if (state.tileLoads === 0 && state.tileErrors >= 4 && !state.tileWarning) {
        state.tileWarning = true;
        setStatus(TILE_MESSAGE, "warning");
      }
    });
  }

  function init(el) {
    var map = L.map(el, {zoomControl: true, worldCopyJump: true, preferCanvas: false});
    state = {map: map, el: el, groups: null, byId: {}, fitKey: null, signature: null, bounds: null, pendingFit: true,
             tileLoads: 0, tileErrors: 0, tileWarning: false, overlayMessage: "", selected: null};
    var bases = basemaps();
    Object.keys(bases).forEach(function (k) {
      bases[k].on("add", function () { state.tileLoads = 0; state.tileErrors = 0; });
      watchTiles(bases[k]);
    });
    bases["USGS Topo"].addTo(map);
    var groups = {
      areas: L.layerGroup().addTo(map),
      roadway: L.layerGroup().addTo(map),
      weather: L.layerGroup().addTo(map),
      seismic: L.layerGroup().addTo(map)
    };
    state.groups = groups;
    L.control.layers(bases, {
      "Warning areas": groups.areas,
      "Roadway incidents": groups.roadway,
      "Weather observations": groups.weather,
      "Earthquakes": groups.seismic
    }, {collapsed: true, position: "topright"}).addTo(map);
    L.control.scale({imperial: true, metric: true}).addTo(map);
    map.setView([39.5, -98.35], 4);
    if (window.ResizeObserver) {
      new ResizeObserver(function () {
        map.invalidateSize();
        if (state.pendingFit) { fit(); }
      }).observe(el);
    }
    return state;
  }

  function fit() {
    if (!state.el.clientWidth || !state.el.clientHeight) {
      state.pendingFit = true;
      return;
    }
    state.map.invalidateSize();
    state.map.fitBounds(state.bounds || US_VIEW, {padding: [28, 28], maxZoom: 8});
    state.pendingFit = false;
  }

  function areaStyle(a, highlighted) {
    var sev = a.severity || 1;
    return {
      color: a.color, weight: highlighted ? 3.5 : 0.8 + sev * 0.35, opacity: a.cleared ? 0.5 : 0.95,
      fillColor: a.color, fillOpacity: a.cleared ? 0.05 : 0.06 + sev * 0.05,
      dashArray: a.cleared ? "4 4" : null
    };
  }

  function pointStyle(p, highlighted) {
    var radius = 4 + 2 * (p.severity || 1);
    if (p.verified) {
      return {radius: radius, color: highlighted ? "#ffffff" : "#0b0c10", weight: highlighted ? 3 : 1.5,
              fillColor: p.color, fillOpacity: 0.95, opacity: 1};
    }
    return {radius: radius, color: p.color, weight: highlighted ? 3.5 : 2, dashArray: "3 3",
            fillColor: p.color, fillOpacity: 0.3, opacity: 1};
  }

  function select(id) {
    if (window.dash_clientside && window.dash_clientside.set_props) {
      window.dash_clientside.set_props("selected_incident", {data: id});
    }
  }

  function draw(payload, mode) {
    var g = state.groups;
    Object.keys(g).forEach(function (k) { g[k].clearLayers(); });
    state.byId = {};

    (payload.areas || []).forEach(function (a) {
      var layer = L.geoJSON(a.geometry, {style: areaStyle(a, false)});
      layer.bindTooltip(a.tooltip, {sticky: true, className: "sauron-tooltip"});
      layer.on("mouseover", function () { layer.setStyle({weight: 3}); });
      layer.on("mouseout", function () { layer.setStyle(areaStyle(a, a.id === state.selected)); });
      layer.on("click", function () { select(a.id); });
      g.areas.addLayer(layer);
      state.byId[a.id] = {layer: layer, kind: "area", item: a};
    });

    ["roadway", "weather", "seismic"].forEach(function (name) {
      var pts = (payload.points || []).filter(function (p) { return p.layer === name; });
      if (!pts.length) { return; }
      if (mode === "heat" && L.heatLayer) {
        g[name].addLayer(L.heatLayer(pts.map(function (p) { return [p.lat, p.lon, (p.severity || 1) / 5]; }),
                                     {radius: 28, blur: 22, maxZoom: 9, minOpacity: 0.35}));
        return;
      }
      var target = g[name];
      if (mode === "cluster" && L.markerClusterGroup) {
        target = L.markerClusterGroup({showCoverageOnHover: false, maxClusterRadius: 45});
        g[name].addLayer(target);
      }
      pts.forEach(function (p) {
        var m = L.circleMarker([p.lat, p.lon], pointStyle(p, false));
        m.bindTooltip(p.tooltip, {className: "sauron-tooltip", direction: "top", offset: [0, -6]});
        m.on("click", function () { select(p.id); });
        target.addLayer(m);
        state.byId[p.id] = {layer: m, kind: "point", item: p};
      });
    });
  }

  function highlight(id) {
    var prev = state.selected && state.byId[state.selected];
    if (prev) {
      prev.layer.setStyle(prev.kind === "area" ? areaStyle(prev.item, false) : pointStyle(prev.item, false));
    }
    state.selected = id;
    var cur = id && state.byId[id];
    if (!cur) { return false; }
    cur.layer.setStyle(cur.kind === "area" ? areaStyle(cur.item, true) : pointStyle(cur.item, true));
    if (cur.layer.bringToFront) { cur.layer.bringToFront(); }
    return true;
  }

  function focus(id) {
    var cur = id && state.byId[id];
    if (!cur) { return; }
    if (cur.kind === "area") {
      state.map.fitBounds(cur.layer.getBounds(), {padding: [30, 30], maxZoom: 10});
    } else {
      state.map.setView(cur.layer.getLatLng(), Math.max(state.map.getZoom(), 9));
    }
    cur.layer.openTooltip();
  }

  window.dash_clientside = Object.assign({}, window.dash_clientside, {
    sauron: {
      renderMap: function (payload, mode, selected) {
        var el = document.getElementById("incident_map");
        if (!el) { return status("", "info"); }
        if (typeof L === "undefined") {
          return status("The map library did not load, so the map cannot be drawn. Reload the page.", "warning");
        }
        if (!state || state.el !== el) { init(el); }
        if (!payload) { return status(state.overlayMessage, "info"); }
        var signature = (payload.signature || "") + "|" + mode;
        if (signature !== state.signature) {
          draw(payload, mode);
          state.signature = signature;
          highlight(state.selected);
        }
        if (payload.fit_key !== state.fitKey) {
          state.fitKey = payload.fit_key;
          state.bounds = payload.bounds;
          fit();
        }
        if (selected !== state.selected) {
          if (highlight(selected)) { focus(selected); }
        }
        var n = (payload.areas || []).length + (payload.points || []).length;
        state.overlayMessage = n ? "" : "No mappable incidents match the current filters. Incidents without a reliable location are listed in the Incidents tab only.";
        if (state.tileWarning) { return status(TILE_MESSAGE, "warning"); }
        return status(state.overlayMessage, "info");
      },
      resizeMap: function (activeTab) {
        if (state && activeTab === "tab-map") {
          setTimeout(function () {
            state.map.invalidateSize();
            if (state.pendingFit) { fit(); }
          }, 30);
        }
        return window.dash_clientside.no_update;
      }
    }
  });
})();
