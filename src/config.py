import os

import yaml

ENV_OVERRIDES = {
    "KAFKA_BOOTSTRAP_SERVERS": ("kafka", "bootstrap_servers"),
    "SAURON_DB_PATH": ("storage", "path"),
    "SAURON_REPORTS_DIR": ("reports", "dir"),
    "SAURON_HOST": ("app", "host"),
    "SAURON_PORT": ("app", "port"),
    "SAURON_METRICS_HOST": ("metrics", "host"),
    "SAURON_METRICS_PORT": ("metrics", "port"),
    "SAURON_LOG_LEVEL": ("logging", "level"),
    "SAURON_LOG_FORMAT": ("logging", "format"),
    "SAURON_TRANSPORT": ("pipeline", "transport"),
}


def load_config(path=None):
    path = path or os.getenv("SAURON_CONFIG", "config/config.yaml")
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    for env, (section, key) in ENV_OVERRIDES.items():
        value = os.getenv(env)
        if value:
            cfg.setdefault(section, {})[key] = int(value) if key == "port" else value
    return cfg


def user_agent(cfg):
    http = cfg.get("http") or {}
    agent = http.get("user_agent", "sauron-hazards/2.0")
    contact = os.getenv(http.get("contact_env", "SAURON_CONTACT_EMAIL"), "")
    return f"{agent} ({contact})" if contact else agent
