import logging
import os
import queue
import threading
import time

from prometheus_client import start_http_server

from . import logs
from .config import load_config, user_agent
from .geocoding import Gazetteer, Geocoder, MemoryCache, Nominatim, load_outlets
from .ingestion.factory import FeedFactory
from .processor import Processor, consume_kafka, consume_queue
from .reports import Reports
from .store import Store

log = logging.getLogger(__name__)


class StoreCache:
    def __init__(self, store):
        self.store = store

    def get(self, provider, key, ttl):
        return self.store.cache_get(provider, key, ttl)

    def put(self, provider, key, value):
        self.store.cache_put(provider, key, value)


def build_geocoder(cfg, store=None):
    geo_cfg = cfg.get("geocoding") or {}
    gazetteer = Gazetteer(geo_cfg.get("gazetteer_path", "data/gazetteer.db"))
    if not gazetteer.available:
        log.warning("gazetteer missing; news locations will stay unresolved. Run: python -m tools.fetch_gazetteer",
                    extra={"path": gazetteer.db_path})
    nominatim = None
    nom_cfg = geo_cfg.get("nominatim") or {}
    if nom_cfg.get("enabled"):
        nominatim = Nominatim(nom_cfg["base_url"], user_agent(cfg), StoreCache(store) if store else MemoryCache(),
                              float(nom_cfg.get("min_interval_secs", 1.1)), int(nom_cfg.get("cache_ttl_days", 30)))
    return Geocoder(gazetteer, load_outlets(geo_cfg.get("outlets_path")), nominatim)


class Pipeline:
    def __init__(self, cfg=None):
        self.cfg = cfg or load_config()
        log_cfg = self.cfg.get("logging") or {}
        logs.configure(log_cfg.get("level", "INFO"), log_cfg.get("format", "json"))
        self.store = Store(self.cfg["storage"]["path"])
        self.reports = Reports(self.store, (self.cfg.get("reports") or {}).get("dir", "reports"))
        self.processor = Processor(self.store, self.cfg, build_geocoder(self.cfg, self.store), self.reports)
        self.stop = threading.Event()
        self.transport = (self.cfg.get("pipeline") or {}).get("transport", "kafka")
        self.queue = queue.Queue(maxsize=10000) if self.transport == "local" else None
        self.kafka = None
        http = self.cfg.get("http") or {}
        self.feeds = [
            FeedFactory.create(name, fcfg, user_agent=user_agent(self.cfg), timeout=int(http.get("timeout_secs", 15)),
                               store=self.store)
            for name, fcfg in (self.cfg.get("feeds") or {}).items()
            if fcfg.get("enabled", True)
        ]

    def publish(self, feed, key, envelope):
        if self.queue is not None:
            self.queue.put(envelope, timeout=5)
        else:
            self.kafka.publish(feed, key, envelope)

    def run(self):
        metrics = self.cfg.get("metrics") or {}
        start_http_server(int(metrics.get("port", 8000)), addr=metrics.get("host", "127.0.0.1"))
        if self.transport == "kafka":
            from .core.kafka_manager import KafkaManager, ensure_topic, make_consumer
            ensure_topic(self.cfg["kafka"])
            self.kafka = KafkaManager(self.cfg["kafka"])
            consumer = make_consumer(self.cfg["kafka"])
            worker = threading.Thread(target=consume_kafka, args=(self.processor, consumer, self.stop),
                                      name="processor", daemon=True)
        else:
            log.warning("local transport: raw events are not durable and cannot be replayed")
            worker = threading.Thread(target=consume_queue, args=(self.processor, self.queue, self.stop),
                                      name="processor", daemon=True)
        worker.start()
        for feed in self.feeds:
            threading.Thread(target=feed.run, args=(self.publish, self.stop), name=f"feed-{feed.name}", daemon=True).start()
        threading.Thread(target=self.maintain, name="maintenance", daemon=True).start()
        from .dashboard import run_dashboard
        try:
            run_dashboard(self.store, self.cfg)
        finally:
            self.stop.set()
            if self.kafka:
                self.kafka.close()

    def maintain(self):
        interval = int((self.cfg.get("pipeline") or {}).get("maintenance_interval_secs", 30))
        export_every = int((self.cfg.get("reports") or {}).get("csv_export_interval_secs", 60))
        last_export = 0.0
        while not self.stop.is_set():
            try:
                self.processor.expire()
                self.processor.refresh_gauges()
                if time.monotonic() - last_export >= export_every:
                    self.reports.export_csv()
                    last_export = time.monotonic()
            except Exception:
                log.exception("maintenance cycle failed")
            self.stop.wait(interval)


def run_demo(cfg=None):
    """Replay saved fixtures into a separate database and serve the dashboard. No network or Kafka."""
    from tools.demo import load_demo
    cfg = cfg or load_config()
    cfg["storage"]["path"] = os.getenv("SAURON_DEMO_DB", "data/demo.db")
    cfg.setdefault("reports", {})["dir"] = os.getenv("SAURON_DEMO_REPORTS", "data/demo-reports")
    logs.configure("INFO", "text")
    store = load_demo(cfg)
    from .dashboard import run_dashboard
    run_dashboard(store, cfg)
