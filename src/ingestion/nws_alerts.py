import logging
import time

from .. import geo
from .base import BaseFeed, FeedError

log = logging.getLogger(__name__)

ZONE_TOLERANCE_DEG = 0.0005


class NWSFeed(BaseFeed):
    """Active NWS alerts in one nationwide request (https://www.weather.gov/documentation/services-web-api).

    Most alerts carry no polygon, only forecast/county zone references. Zone
    geometries are fetched once and cached; an alert is published only after
    its zones are available (or after max_defer_polls, without geometry).
    """

    kind = "nws"

    def __init__(self, name, cfg, **kwargs):
        super().__init__(name, cfg, **kwargs)
        self.url = cfg.get("url", "https://api.weather.gov/alerts/active")
        self.events = set((cfg.get("events") or {}).keys())
        self.zone_fetch_limit = int(cfg.get("zone_fetch_limit", 40))
        self.max_defer_polls = int(cfg.get("max_defer_polls", 10))
        self._deferred = {}
        self._zone_failures = {}

    def fetch(self):
        params = {"status": "actual", "message_type": "alert,update"}
        if self.cfg.get("region_type"):
            params["region_type"] = self.cfg["region_type"]
        return self.get_json(self.url, params=params, headers={"Accept": "application/geo+json"}, conditional=True)

    def parse(self, data):
        if not isinstance(data, dict) or data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list):
            raise FeedError("parse", "response is not a GeoJSON FeatureCollection")
        items = []
        for feature in data["features"]:
            if not isinstance(feature, dict) or not (feature.get("properties") or {}).get("id"):
                raise FeedError("parse", "alert feature without properties.id")
            items.append(feature)
        return items

    def item_id(self, item):
        return item["properties"]["id"]

    def snapshot(self, items):
        return {"active_ids": [self.item_id(i) for i in items]}

    def prepare(self, items):
        if self.store is None:
            return items
        budget = self.zone_fetch_limit
        ready = []
        for item in items:
            props = item["properties"]
            if props.get("event") not in self.events or item.get("geometry"):
                ready.append(item)
                continue
            zones = [z.rstrip("/") for z in props.get("affectedZones") or []]
            missing = [z for z in zones if not self.store.zone_geometry(z.rsplit("/", 1)[-1])]
            for url in missing:
                if budget <= 0:
                    break
                if time.time() - self._zone_failures.get(url, 0) < 3600:
                    continue
                budget -= 1
                self._fetch_zone(url)
            missing = [z for z in zones if not self.store.zone_geometry(z.rsplit("/", 1)[-1])]
            alert_id = props["id"]
            if missing and self._deferred.get(alert_id, 0) < self.max_defer_polls:
                self._deferred[alert_id] = self._deferred.get(alert_id, 0) + 1
                continue
            self._deferred.pop(alert_id, None)
            ready.append(item)
        return ready

    def _fetch_zone(self, url):
        zone_id = url.rsplit("/", 1)[-1]
        try:
            data = self.get_json(url, headers={"Accept": "application/geo+json"})
            geometry = geo.merge([data.get("geometry")]) if data.get("geometry") else None
            if not geometry:
                raise FeedError("parse", f"zone {zone_id} has no polygon")
            self.store.cache_put("nws_zone", zone_id, geo.round_geometry(geo.simplify(geometry, ZONE_TOLERANCE_DEG)))
        except FeedError as exc:
            self._zone_failures[url] = time.time()
            log.warning("zone geometry unavailable", extra={"feed": self.name, "zone": zone_id,
                                                            "error_category": exc.category, "error": str(exc)[:200]})
