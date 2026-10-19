"""Only a confirmed missing version tag permits publishing a new image."""
import os
import urllib.error
import urllib.request
from release_version import release_version

repository = os.environ["GITHUB_REPOSITORY"]
if repository != "niki11euro/home-assistant-lexware-upload-app":
    raise SystemExit("Publishing is restricted to the app repository")
url = "https://api.github.com/repos/" + repository + "/git/ref/tags/v" + release_version()
request = urllib.request.Request(url, headers={
    "Authorization": "Bearer " + os.environ["GH_TOKEN"],
    "Accept": "application/vnd.github+json",
    "User-Agent": "wer-lexware-upload-release",
    "X-GitHub-Api-Version": "2022-11-28",
})
try:
    with urllib.request.urlopen(request, timeout=30):
        print("publish=false")
except urllib.error.HTTPError as error:
    if error.code != 404:
        raise SystemExit("Cannot verify release tag; refusing to publish") from None
    print("publish=true")
except urllib.error.URLError:
    raise SystemExit("Cannot verify release tag; refusing to publish") from None
