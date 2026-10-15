from .nws_alerts import NWSFeed
from .open_data import OpenDataFeed
from .open_meteo import OpenMeteoFeed
from .rss_feed import RSSFeed
from .usgs_quakes import USGSFeed

_MAP = {
    "nws": NWSFeed,
    "open_meteo": OpenMeteoFeed,
    "usgs": USGSFeed,
    "open_data": OpenDataFeed,
    "rss": RSSFeed,
}

_VALID = ", ".join(sorted(_MAP))


class FeedFactory:
    @staticmethod
    def create(name, cfg, **kwargs):
        """Create a feed from its config entry; the config key is the feed name."""
        if not isinstance(cfg, dict):
            raise TypeError(f"feed config must be a dict, got {type(cfg).__name__}")
        kind = (cfg.get("type") or "").strip().lower()
        if not kind:
            raise ValueError(f"feed {name!r} is missing 'type'")
        cls = _MAP.get(kind)
        if not cls:
            raise ValueError(f"unknown feed type {kind!r} for {name!r}; valid types: {_VALID}")
        return cls(name, cfg, **kwargs)
