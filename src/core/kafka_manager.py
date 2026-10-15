import json
import logging

from kafka import KafkaConsumer, KafkaProducer
from kafka.admin import KafkaAdminClient, NewTopic
from kafka.errors import TopicAlreadyExistsError

from ..metrics import publish_errors, published

log = logging.getLogger(__name__)


class KafkaManager:
    """Producer for the raw topic. The broker acknowledges a write only once it is replicated (acks=all)."""

    def __init__(self, cfg):
        self.topic_raw = cfg["topic_raw"]
        self.bootstrap = cfg["bootstrap_servers"]
        self.producer = KafkaProducer(
            bootstrap_servers=self.bootstrap,
            client_id=cfg.get("client_id", "sauron"),
            acks="all",
            retries=5,
            linger_ms=20,
            max_block_ms=10000,
            key_serializer=lambda k: k.encode() if k else None,
            value_serializer=lambda v: json.dumps(v, separators=(",", ":")).encode(),
        )

    def publish(self, feed, key, envelope):
        future = self.producer.send(self.topic_raw, key=key, value=envelope)
        future.add_callback(lambda _: published.labels(feed).inc())
        future.add_errback(lambda exc: self._failed(feed, key, exc))

    def _failed(self, feed, key, exc):
        publish_errors.labels(feed).inc()
        log.error("raw publish failed", extra={"feed": feed, "key": key, "error": repr(exc)})

    def close(self):
        self.producer.flush(10)
        self.producer.close(10)


def ensure_topic(cfg):
    """Create the raw topic if it is missing, so the consumer never waits on metadata for it."""
    admin = KafkaAdminClient(bootstrap_servers=cfg["bootstrap_servers"], client_id=cfg.get("client_id", "sauron") + "-admin")
    try:
        if cfg["topic_raw"] in admin.list_topics():
            return False
        admin.create_topics([NewTopic(cfg["topic_raw"], num_partitions=int(cfg.get("partitions", 1)),
                                      replication_factor=int(cfg.get("replication_factor", 1)))])
        log.info("created topic", extra={"topic": cfg["topic_raw"]})
        return True
    except TopicAlreadyExistsError:
        return False
    finally:
        admin.close()


def make_consumer(cfg):
    return KafkaConsumer(
        cfg["topic_raw"],
        bootstrap_servers=cfg["bootstrap_servers"],
        group_id=cfg.get("group_id", "sauron-processor"),
        client_id=cfg.get("client_id", "sauron") + "-processor",
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        max_poll_records=200,
    )
