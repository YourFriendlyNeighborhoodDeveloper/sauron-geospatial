from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .base import BaseFeed, FeedError


class OpenDataFeed(BaseFeed):
    """DataSF police incident reports (Socrata), traffic collisions only.

    Records arrive one to three days after the incident, so a short lookback
    window would always come back empty.
    """

    kind = "open_data"

    def fetch(self):
        tz = ZoneInfo(self.cfg.get("timezone", "America/Los_Angeles"))
        since = (datetime.now(tz) - timedelta(days=int(self.cfg.get("lookback_days", 7)))).strftime("%Y-%m-%dT%H:%M:%S")
        params = {
            "$where": f"incident_category = 'Traffic Collision' AND incident_datetime > '{since}'",
            "$order": "incident_datetime DESC",
            "$limit": int(self.cfg.get("limit", 500)),
        }
        return self.get_json(self.cfg["url"], params=params)

    def parse(self, data):
        if not isinstance(data, list):
            raise FeedError("parse", "expected a JSON array of rows")
        return data

    def item_id(self, item):
        return item.get("row_id")
