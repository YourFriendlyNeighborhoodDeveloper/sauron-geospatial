"""End-to-end against a real broker. Skipped unless SAURON_KAFKA_TEST=1.

    docker compose up -d kafka
    SAURON_KAFKA_TEST=1 pytest tests/integration -q
"""
import json
import os
import threading
import time
import uuid

import pytest

pytestmark = pytest.mark.integration

if os.getenv("SAURON_KAFKA_TEST") != "1":
    pytest.skip("set SAURON_KAFKA_TEST=1 with a broker running to run integration tests", allow_module_level=True)

from kafka import KafkaConsumer, TopicPartition  # noqa: E402
from kafka.admin import KafkaAdminClient, NewTopic  # noqa: E402

from src.core.kafka_manager import KafkaManager  # noqa: E402
from src.processor import consume_kafka  # noqa: E402
from tools.demo import fixture_log  # noqa: E402

BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9092")


@pytest.fixture
def topic():
    name = f"sauron-it-{uuid.uuid4().hex[:8]}"
    admin = KafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    admin.create_topics([NewTopic(name, num_partitions=1, replication_factor=1)])
    yield name
    admin.delete_topics([name])
    admin.close()


def publish_all(topic, messages):
    producer = KafkaManager({"topic_raw": topic, "bootstrap_servers": BOOTSTRAP})
    for m in messages:
        env = json.loads(m)
        producer.publish(env["feed"], None, env)
    producer.close()


def run_consumer(processor, topic, group, expect_end):
    consumer = KafkaConsumer(topic, bootstrap_servers=BOOTSTRAP, group_id=group, enable_auto_commit=False,
                             auto_offset_reset="earliest")
    stop = threading.Event()
    seen = []
    original = processor.handle

    def counting(message, received_at=None, location=None):
        seen.append(location)
        return original(message, received_at, location)
    processor.handle = counting
    worker = threading.Thread(target=consume_kafka, args=(processor, consumer, stop))
    worker.start()
    tp = TopicPartition(topic, 0)
    deadline = time.time() + 60
    while time.time() < deadline:
        committed = consumer.committed(tp) or 0
        if committed >= expect_end:
            break
        time.sleep(0.5)
    time.sleep(1)
    stop.set()
    worker.join(10)
    committed = consumer.committed(tp)
    consumer.close()
    processor.handle = original
    return seen, committed


def snapshot(store):
    return (store.read("SELECT event_id, version, content_hash, incident_id FROM source_events ORDER BY event_id, version"),
            store.read("SELECT incident_id, state, clear_reason, evidence_count, expires_at FROM incidents ORDER BY incident_id"))


def test_roundtrip_restart_and_replay(processor, store, topic):
    log = fixture_log()
    publish_all(topic, log)
    group = f"g-{uuid.uuid4().hex[:6]}"

    seen, committed = run_consumer(processor, topic, group, len(log))
    assert committed == len(log) and len(seen) == len(log)
    first = snapshot(store)
    assert first[0] and first[1]

    seen, committed = run_consumer(processor, topic, group, len(log))
    assert seen == [] and committed == len(log), "restart with the same group must not reprocess"

    publish_all(topic, log)
    seen, committed = run_consumer(processor, topic, group, 2 * len(log))
    assert len(seen) == len(log) and committed == 2 * len(log)
    assert snapshot(store) == first, "republished raw events must be deduplicated"

    seen, _ = run_consumer(processor, topic, f"replay-{uuid.uuid4().hex[:6]}", 2 * len(log))
    assert len(seen) == 2 * len(log)
    assert snapshot(store) == first, "a full replay must leave the store unchanged"
