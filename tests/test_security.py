"""Static checks on the deployable configuration and the tracked files."""
import os
import re
import subprocess

import yaml

from src.config import load_config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tracked_files():
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return [p for p in out.splitlines() if os.path.isfile(os.path.join(ROOT, p))]


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8", errors="replace") as f:
        return f.read()


def test_every_published_port_is_loopback_only():
    compose = yaml.safe_load(read("docker-compose.yml"))
    for name, svc in compose["services"].items():
        for port in svc.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name} publishes {port} beyond loopback"


def test_only_the_expected_services_exist():
    compose = yaml.safe_load(read("docker-compose.yml"))
    assert set(compose["services"]) == {"kafka", "kafka-init", "app", "prometheus", "grafana"}


def test_no_default_credentials_in_compose_or_config():
    text = read("docker-compose.yml") + read("config/config.yaml")
    assert not re.search(r"(?i)(password|secret|token)\s*[:=]\s*['\"]?[A-Za-z0-9]{3,}", text)
    assert "neo4j/password" not in text and "ALLOW_ANONYMOUS_LOGIN" not in text


def test_grafana_has_no_usable_default_admin():
    env = yaml.safe_load(read("docker-compose.yml"))["services"]["grafana"]["environment"]
    assert env["GF_AUTH_DISABLE_LOGIN_FORM"] == "true" and env["GF_AUTH_BASIC_ENABLED"] == "false"


def test_app_defaults_listen_on_loopback():
    cfg = load_config(os.path.join(ROOT, "config", "config.yaml"))
    assert cfg["app"]["host"] == "127.0.0.1" and cfg["metrics"]["host"] == "127.0.0.1"


def test_no_hardcoded_proxy():
    assert "docker.internal:3128" not in read("docker-compose.yml")


def test_container_runs_as_non_root():
    assert re.search(r"^USER\s+sauron\s*$", read("Dockerfile"), re.M)


def test_secrets_files_are_ignored():
    ignore = read(".gitignore").splitlines()
    for entry in (".env", "data/", "reports/", "archive/"):
        assert entry in ignore
    assert ".env" not in tracked_files()
    for line in read(".env.example").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            if any(s in key for s in ("PASSWORD", "TOKEN", "KEY")):
                assert value == "", f"{key} must be blank in .env.example"


def test_no_personal_contact_enrichment_in_code():
    pattern = re.compile(r"peopledatalabs|personal_emails|personal_numbers|person/enrich", re.I)
    for path in tracked_files():
        if path.endswith((".py", ".yaml", ".yml", ".json", ".md", ".txt")) and not path.startswith("tests/test_security"):
            assert not pattern.search(read(path)), f"personal-data enrichment reference in {path}"


def test_no_api_keys_or_private_keys_in_tracked_files():
    patterns = [
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"(?i)(?:api[_-]?key|apikey)\s*[=:]\s*['\"][A-Za-z0-9_\-]{16,}['\"]"),
        re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    ]
    for path in tracked_files():
        if path.endswith((".png", ".jpg", ".zip", ".db")):
            continue
        text = read(path)
        for p in patterns:
            assert not p.search(text), f"possible secret in {path}"
