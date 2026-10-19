"""Never overwrite a published version if release creation was interrupted."""
import json
import os
import subprocess
import urllib.error
import urllib.request
from release_version import release_version

image = "ghcr.io/niki11euro/home-assistant-lexware-upload-app"
request = urllib.request.Request(
    "https://api.github.com/users/niki11euro/packages/container/home-assistant-lexware-upload-app",
    headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
             "Accept": "application/vnd.github+json", "User-Agent": "wer-app-image-check"})
try:
    with urllib.request.urlopen(request, timeout=30):
        pass
except urllib.error.HTTPError as error:
    if error.code == 404:
        print("build=true")
        raise SystemExit(0)
    raise SystemExit("Cannot inspect package; refusing to publish") from None
except urllib.error.URLError:
    raise SystemExit("Cannot inspect package; refusing to publish") from None

result = subprocess.run(["docker", "buildx", "imagetools", "inspect", "--raw",
                         image + ":" + release_version()], capture_output=True, text=True)
if result.returncode:
    if "not found" in result.stderr.lower() or "manifest unknown" in result.stderr.lower():
        print("build=true")
        raise SystemExit(0)
    raise SystemExit("Cannot inspect image tag; refusing to publish")
manifest = json.loads(result.stdout)
revision = manifest.get("annotations", {}).get("org.opencontainers.image.revision")
if revision != os.environ["GITHUB_SHA"]:
    raise SystemExit("This image version already exists for another commit. Increase config.yaml version.")
print("build=false")
