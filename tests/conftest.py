import copy
import json
import os
from datetime import datetime, timezone

import pytest
import requests

from src.config import load_config
from src.geocoding import Gazetteer, Geocoder, build_gazetteer, load_outlets
from src.processor import Processor
from src.reports import Reports
from src.store import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f) if name.endswith(".json") else f.read()


@pytest.fixture(scope="session")
def gazetteer_db(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("gaz") / "gazetteer.db")
    build_gazetteer({kind: os.path.join(FIXTURES, "gazetteer", f"{kind}.txt") for kind in ("place", "cousub", "county")}, path)
    return path


@pytest.fixture
def geocoder(gazetteer_db):
    return Geocoder(Gazetteer(gazetteer_db), load_outlets(os.path.join(ROOT, "config", "outlets.yaml")))


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(load_config(os.path.join(ROOT, "config", "config.yaml")))
    c["storage"]["path"] = str(tmp_path / "sauron.db")
    c["reports"]["dir"] = str(tmp_path / "reports")
    return c


@pytest.fixture
def store(cfg):
    return Store(cfg["storage"]["path"])


class Clock:
    def __init__(self, iso):
        self.now = datetime.fromisoformat(iso).astimezone(timezone.utc)

    def __call__(self):
        return self.now

    def set(self, iso):
        self.now = datetime.fromisoformat(iso).astimezone(timezone.utc)


@pytest.fixture
def clock():
    return Clock("2026-10-15T17:00:00+00:00")


@pytest.fixture
def processor(store, cfg, geocoder, clock):
    for zone in os.listdir(os.path.join(FIXTURES, "nws_zones")):
        store.cache_put("nws_zone", zone[:-5], fixture(f"nws_zones/{zone}")["geometry"])
    return Processor(store, cfg, geocoder, Reports(store, cfg["reports"]["dir"]), clock=clock)


def envelope(kind, item, feed=None, fetched_at="2026-10-15T16:59:00+00:00"):
    return {"v": 2, "feed": feed or kind, "kind": kind, "fetched_at": fetched_at, "item": item}


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, content=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.content = content if content is not None else (json.dumps(body).encode() if body is not None else b"")
        self.text = self.content.decode("utf-8", "replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        if self._body is None:
            return json.loads(self.text)
        return self._body


class FakeSession:
    """Stands in for requests.Session: returns queued responses or raises queued exceptions."""

    def __init__(self, routes=None):
        self.headers = {}
        self.routes = routes or {}
        self.calls = []

    def mount(self, *args):
        pass

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        for prefix, result in self.routes.items():
            if url.startswith(prefix):
                value = result.pop(0) if isinstance(result, list) else result
                if isinstance(value, Exception):
                    raise value
                return value
        raise requests.ConnectionError(f"no route for {url}")


class Collector:
    def __init__(self):
        self.messages = []

    def __call__(self, feed, key, envelope):
        self.messages.append((feed, key, envelope))
