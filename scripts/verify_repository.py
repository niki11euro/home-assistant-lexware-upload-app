"""Check the published app layout, release metadata and safe defaults."""
import json
from pathlib import Path
import re
import subprocess
import sys
import yaml
from release_version import release_version

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "lexware_upload_bridge"
version = release_version()
config = yaml.safe_load((APP / "config.yaml").read_text())
repo = yaml.safe_load((ROOT / "repository.yaml").read_text())
assert repo["url"] == "https://github.com/niki11euro/home-assistant-lexware-upload-app"
assert config["slug"] == APP.name
assert set(config["arch"]) == {"aarch64", "amd64"}
assert config["image"] == "ghcr.io/niki11euro/home-assistant-lexware-upload-app"
assert config["ingress"] and not config.get("ports")
assert not config.get("host_network") and not config.get("full_access")
assert config["options"]["upload_backend"] == "api"
assert not config["options"]["scan_enabled"]
assert not config["options"]["upload_enabled"]
assert not config["options"]["import_archive_history"]
for key in ("lexware_api_key", "api_token", "mcp_token"):
    assert config["options"][key] == ""
    assert config["schema"][key] == "password"
assert config["options"]["scanner_device_id"] == ""
runtime_version = subprocess.check_output([sys.executable, "-c", "import bridge; print(bridge.VERSION)"], cwd=APP, text=True).strip()
assert runtime_version == version
docker = (APP / "Dockerfile").read_text()
assert "ARG BUILD_VERSION\n" in docker
assert re.search(r"^FROM python:3\.13-slim-bookworm@sha256:[a-f0-9]{64}$", docker, re.M)
assert "archive_import.py" in docker
prepared = json.loads((APP / "options.prepared.json").read_text())
assert prepared["scanner_device_id"] == "example-scanner-id"
assert prepared["scanserv_url"] == "http://scanserver:8080"
subprocess.run([sys.executable, "run.py", "--check-config", "--options", "options.prepared.json"], cwd=APP, check=True)
for name in ("README.md", "DOCS.md", "CHANGELOG.md", "icon.png", "logo.png"):
    assert (APP / name).is_file(), name
assert (ROOT / "LICENSE").is_file()
print("Repository layout, version " + version + ", ingress and safe defaults verified")
