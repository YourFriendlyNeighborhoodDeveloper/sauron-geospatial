from .base import BaseFeed, FeedError


class USGSFeed(BaseFeed):
    """USGS real-time earthquake GeoJSON summary feed."""

    kind = "usgs"

    def fetch(self):
        url = self.cfg.get("url", "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson")
        return self.get_json(url)

    def parse(self, data):
        if not isinstance(data, dict) or data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list):
            raise FeedError("parse", "response is not a GeoJSON FeatureCollection")
        return data["features"]

    def item_id(self, item):
        return item.get("id")
