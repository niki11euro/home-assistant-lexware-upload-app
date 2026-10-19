"""Read the strict release version without importing app dependencies."""
from pathlib import Path
import re


def release_version():
    config = Path(__file__).resolve().parents[1] / "lexware_upload_bridge/config.yaml"
    match = re.search(r"^version:\s*['\"]?(\d+\.\d+\.\d+)['\"]?\s*$", config.read_text(), re.M)
    if match is None:
        raise SystemExit("config.yaml requires a semantic version")
    return match.group(1)


if __name__ == "__main__":
    print(release_version())
