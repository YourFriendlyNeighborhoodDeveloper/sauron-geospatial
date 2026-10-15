import calendar
from datetime import datetime, timezone

import feedparser

from ..schema import utc_iso
from .base import BaseFeed, FeedError


class RSSFeed(BaseFeed):
    kind = "rss"

    def fetch(self):
        return self.get(self.cfg["url"]).content

    def parse(self, raw):
        parsed = feedparser.parse(raw)
        if parsed.bozo and not parsed.entries:
            raise FeedError("parse", f"unreadable feed: {parsed.get('bozo_exception')!r}")
        items = []
        for e in parsed.entries:
            stamp = e.get("published_parsed") or e.get("updated_parsed")
            source = e.get("source") or {}
            items.append({
                "guid": e.get("id") or e.get("link"),
                "title": e.get("title"),
                "link": e.get("link"),
                "published": utc_iso(datetime.fromtimestamp(calendar.timegm(stamp), tz=timezone.utc)) if stamp else None,
                "source_name": source.get("title"),
                "source_url": source.get("href"),
            })
        return items

    def item_id(self, item):
        return item.get("guid")
