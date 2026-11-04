"""Downloads Firefox from Mozilla's release archive into /opt/firefox, only if the archive's SHA-256 is the pinned one.
Run by the Dockerfile's `firefox` stage with FIREFOX_VERSION and FIREFOX_SHA256 in the environment."""
import hashlib
import os
import tarfile
import urllib.request

version = os.environ["FIREFOX_VERSION"]
url = f"https://archive.mozilla.org/pub/firefox/releases/{version}/linux-x86_64/en-US/firefox-{version}.tar.xz"
data = urllib.request.urlopen(url, timeout=300).read()
got = hashlib.sha256(data).hexdigest()
if got != os.environ["FIREFOX_SHA256"]:
    raise SystemExit(f"firefox {version}: sha256 is {got}, pinned {os.environ['FIREFOX_SHA256']}")
with open("/tmp/firefox.tar.xz", "wb") as f:
    f.write(data)
with tarfile.open("/tmp/firefox.tar.xz") as archive:
    archive.extractall("/opt", filter="data")
