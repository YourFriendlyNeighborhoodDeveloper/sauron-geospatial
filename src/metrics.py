from prometheus_client import Counter, Gauge, Histogram

feed_polls = Counter("sauron_feed_polls_total", "Feed poll attempts", ["feed", "outcome"])
feed_errors = Counter("sauron_feed_errors_total", "Feed failures by category", ["feed", "category"])
feed_last_success = Gauge("sauron_feed_last_success_timestamp_seconds", "Unix time of the last successful poll", ["feed"])
feed_poll_seconds = Histogram("sauron_feed_poll_duration_seconds", "Source response time per poll", ["feed"],
                              buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 60))
feed_items = Counter("sauron_feed_items_total", "Items received from sources", ["feed"])
feed_items_last_poll = Gauge("sauron_feed_items_last_poll", "Items in the most recent successful poll", ["feed"])
published = Counter("sauron_raw_published_total", "Raw envelopes published to the log", ["feed"])
publish_errors = Counter("sauron_raw_publish_errors_total", "Raw envelopes the broker did not accept", ["feed"])

events_normalized = Counter("sauron_events_normalized_total", "Source events that passed validation", ["source"])
events_rejected = Counter("sauron_events_rejected_total", "Items that failed parsing or validation", ["source", "reason"])
events_filtered = Counter("sauron_events_filtered_total", "Valid items outside SAURON's scope", ["source", "reason"])
duplicates = Counter("sauron_duplicates_suppressed_total", "Repeated items dropped", ["stage"])
processing_errors = Counter("sauron_processing_errors_total", "Unexpected processing failures", ["stage"])
dead_letters = Counter("sauron_dead_letters_total", "Messages stored for inspection after failing", ["source"])
geocode_requests = Counter("sauron_geocode_total", "Geocoder lookups", ["provider", "outcome"])

incidents_active = Gauge("sauron_incidents_active", "Open incidents", ["category"])
incident_transitions = Counter("sauron_incident_transitions_total", "Incident state changes", ["to_state", "reason"])
consumer_lag = Gauge("sauron_consumer_lag_messages", "Messages behind the end of the raw topic", ["partition"])
processing_seconds = Histogram("sauron_event_processing_seconds", "Time to process one raw envelope",
                               buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 1))
availability_latency = Histogram(
    "sauron_publication_to_available_seconds",
    "Source publication to queryable in SAURON, for records published after the processor started",
    ["source"],
    buckets=(5, 15, 30, 60, 120, 300, 600, 1800, 3600, 7200, 21600, 86400),
)
