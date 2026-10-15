import hashlib
import json
import logging
import random
import time
from collections import OrderedDict
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..metrics import (
    duplicates, feed_errors, feed_items, feed_items_last_poll, feed_last_success, feed_poll_seconds, feed_polls,
)
from ..schema import utc_iso

log = logging.getLogger(__name__)


class FeedError(Exception):
    def __init__(self, category, message):
        super().__init__(message)
        self.category = category


class NotModified(Exception):
    pass


class BaseFeed:
    kind = None

    def __init__(self, name, cfg, user_agent="sauron-hazards/2.0", timeout=15, store=None, session=None):
        self.name = name
        self.cfg = cfg
        self.poll_interval = int(cfg.get("poll_interval", 60))
        self.timeout = timeout
        self.store = store
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = user_agent
        retry = Retry(total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=("GET",), respect_retry_after_header=True)
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self._seen = OrderedDict()
        self._etags = {}
        self.failures = 0

    def get(self, url, params=None, headers=None, conditional=False):
        hdrs = dict(headers or {})
        if conditional and url in self._etags:
            hdrs["If-None-Match"] = self._etags[url]
        try:
            resp = self.session.get(url, params=params, headers=hdrs, timeout=self.timeout)
        except requests.Timeout as exc:
            raise FeedError("timeout", str(exc)) from exc
        except requests.ConnectionError as exc:
            raise FeedError("connection", str(exc)) from exc
        except requests.RequestException as exc:
            raise FeedError("http", str(exc)) from exc
        if resp.status_code == 304:
            raise NotModified()
        if resp.status_code >= 400:
            raise FeedError(f"http_{resp.status_code}", resp.text[:300])
        if conditional and resp.headers.get("ETag"):
            self._etags[url] = resp.headers["ETag"]
        return resp

    def get_json(self, url, **kwargs):
        resp = self.get(url, **kwargs)
        try:
            return resp.json()
        except ValueError as exc:
            raise FeedError("parse", f"invalid JSON from {url}: {exc}") from exc

    def fetch(self):
        raise NotImplementedError

    def parse(self, raw):
        raise NotImplementedError

    def item_id(self, item):
        return None

    def prepare(self, items):
        """Hook for feeds that must fetch supporting data before publishing."""
        return items

    def snapshot(self, items):
        return None

    def poll_once(self, publish):
        started = time.monotonic()
        at = utc_iso(datetime.now(timezone.utc))
        try:
            items = self.parse(self.fetch())
            ready = self.prepare(items)
        except NotModified:
            feed_polls.labels(self.name, "not_modified").inc()
            feed_last_success.labels(self.name).set(time.time())
            self._record(at, True, 0, 0)
            self.failures = 0
            return {"outcome": "not_modified", "items": 0, "published": 0}
        except FeedError as exc:
            return self._failed(at, exc.category, str(exc), started)
        except Exception as exc:
            log.exception("feed poll crashed", extra={"feed": self.name})
            return self._failed(at, "internal", repr(exc), started)
        feed_poll_seconds.labels(self.name).observe(time.monotonic() - started)

        published = 0
        for item in ready:
            digest = hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()
            if digest in self._seen:
                self._seen.move_to_end(digest)
                duplicates.labels("feed").inc()
                continue
            envelope = {"v": 2, "feed": self.name, "kind": self.kind, "fetched_at": at, "item": item}
            native = self.item_id(item)
            try:
                publish(self.name, f"{self.kind}:{native}" if native else None, envelope)
            except Exception as exc:
                return self._failed(at, "publish", repr(exc), started)
            self._seen[digest] = True
            published += 1
        while len(self._seen) > 50000:
            self._seen.popitem(last=False)
        snap = self.snapshot(items)
        if snap is not None:
            try:
                publish(self.name, f"{self.kind}:snapshot", {"v": 2, "feed": self.name, "kind": self.kind,
                                                           "fetched_at": at, "snapshot": snap})
            except Exception as exc:
                return self._failed(at, "publish", repr(exc), started)
        feed_polls.labels(self.name, "success").inc()
        feed_items.labels(self.name).inc(len(items))
        feed_items_last_poll.labels(self.name).set(len(items))
        feed_last_success.labels(self.name).set(time.time())
        self.failures = 0
        self._record(at, True, len(items), published)
        log.info("feed polled", extra={"feed": self.name, "items": len(items), "published": published})
        return {"outcome": "success", "items": len(items), "published": published}

    def _failed(self, at, category, message, started):
        self.failures += 1
        feed_polls.labels(self.name, "error").inc()
        feed_errors.labels(self.name, category).inc()
        feed_poll_seconds.labels(self.name).observe(time.monotonic() - started)
        log.warning("feed poll failed", extra={"feed": self.name, "error_category": category,
                                               "error": message[:300], "consecutive_failures": self.failures})
        self._record(at, False, error_category=category, error=message)
        return {"outcome": "error", "category": category, "error": message}

    def _record(self, at, ok, items=None, published=None, error_category=None, error=None):
        if self.store is None:
            return
        try:
            self.store.record_poll(self.name, self.kind, self.poll_interval, at, ok, items, published,
                                   error_category, error)
        except Exception:
            log.exception("could not record feed status", extra={"feed": self.name})

    def next_delay(self):
        if self.failures:
            return min(self.poll_interval * 2 ** min(self.failures, 5), 1800) * random.uniform(0.9, 1.1)
        return self.poll_interval * random.uniform(0.95, 1.05)

    def run(self, publish, stop):
        while not stop.is_set():
            self.poll_once(publish)
            stop.wait(self.next_delay())
