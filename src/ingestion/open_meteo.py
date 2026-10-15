import os

from .base import BaseFeed, FeedError

CURRENT = "temperature_2m,precipitation,rain,showers,snowfall,weather_code,wind_speed_10m,wind_gusts_10m,visibility"


class OpenMeteoFeed(BaseFeed):
    """Current model conditions at configured points (https://open-meteo.com/en/docs).

    Units are requested explicitly and checked against current_units in the
    response. The free endpoint is for non-commercial use; with an API key
    the commercial endpoint is used instead.
    """

    kind = "open_meteo"

    def __init__(self, name, cfg, **kwargs):
        super().__init__(name, cfg, **kwargs)
        self.api_key = os.getenv(cfg.get("api_key_env", "OPEN_METEO_API_KEY"), "")
        host = "https://customer-api.open-meteo.com" if self.api_key else "https://api.open-meteo.com"
        self.url = cfg.get("url", f"{host}/v1/forecast")

    def fetch(self):
        out, errors = [], []
        for point in self.cfg.get("points") or []:
            params = {
                "latitude": point["lat"], "longitude": point["lon"], "current": CURRENT,
                "wind_speed_unit": "kmh", "precipitation_unit": "mm", "temperature_unit": "celsius",
                "timezone": "UTC",
            }
            if self.api_key:
                params["apikey"] = self.api_key
            try:
                data = self.get_json(self.url, params=params)
            except FeedError as exc:
                errors.append(f"{point.get('name')}: {exc.category}")
                continue
            out.append({"point": point, "data": data})
        if errors and not out:
            raise FeedError("all_points_failed", "; ".join(errors))
        return out

    def parse(self, raw):
        items = []
        for entry in raw:
            data = entry["data"]
            if not isinstance(data, dict) or not isinstance(data.get("current"), dict):
                raise FeedError("parse", f"no current block for {entry['point'].get('name')}")
            items.append({
                "point": entry["point"],
                "current": data["current"],
                "units": data.get("current_units") or {},
                "grid": {"lat": data.get("latitude"), "lon": data.get("longitude"), "elevation": data.get("elevation")},
            })
        return items

    def item_id(self, item):
        return f"{item['point'].get('name')}:{item['current'].get('time')}"
