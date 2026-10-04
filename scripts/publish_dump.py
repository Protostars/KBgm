"""Publish immutable incremental Bangumi packages; never connect to a server.

Published release manifests are the authoritative publication history. The
committed data/latest.json is only a recoverable, human-readable mirror.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import urljoin, urlparse

import requests

try:
    from .build_dump import build_package, fetch_latest, parse_dump_version
except ImportError:
    from build_dump import build_package, fetch_latest, parse_dump_version


ROOT = Path(__file__).resolve().parents[1]
MARKER = "<!-- kbgm-publisher:v1 -->"
ASSET_NAMES = {"bgm_extracted.zip", "manifest.json"}
API_HOSTS = {"api.github.com", "uploads.github.com"}
MAX_MANIFEST_BYTES = 1024 * 1024


def strict_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key: " + key)
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("Invalid JSON constant: " + value)

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)


def iso_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Expected an ISO date (YYYY-MM-DD)")
    return dt.date.fromisoformat(value)


def version_key(value):
    parse_dump_version(value)
    return dt.datetime.strptime(value, "dump-%Y-%m-%d.%H%M%SZ")


def positive_integer(value):
    return type(value) is int and value > 0


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_manifest(manifest, version):
    if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int:
        raise ValueError("Invalid manifest schema_version")
    if manifest["schema_version"] != 1 or manifest.get("dump_version") != version:
        raise ValueError("Manifest schema or dump_version does not match its release")
    dump_date = parse_dump_version(version)
    if "previous_version" not in manifest:
        raise ValueError("Manifest is missing previous_version")
    previous = manifest["previous_version"]
    if previous is not None and version_key(previous) >= version_key(version):
        raise ValueError("Manifest previous_version must be older than dump_version")
    if iso_date(manifest.get("since")) > dump_date:
        raise ValueError("Manifest since cannot be later than the dump date")
    generated = manifest.get("generated_at")
    if not isinstance(generated, str):
        raise ValueError("Manifest is missing generated_at")
    generated_date = dt.datetime.fromisoformat(generated.replace("Z", "+00:00"))
    if generated_date.tzinfo is None:
        raise ValueError("Manifest generated_at must contain a timezone")
    for field, name in (("archive", "bgm_extracted.zip"), ("source", version + ".zip")):
        metadata = manifest.get(field)
        if not isinstance(metadata, dict) or metadata.get("name") != name:
            raise ValueError("Invalid manifest " + field + " filename")
        digest = metadata.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid manifest " + field + " SHA-256")
        if not positive_integer(metadata.get("size")):
            raise ValueError("Invalid manifest " + field + " size")
    source = urlparse(manifest["source"].get("url", ""))
    if (source.scheme != "https" or source.hostname != "github.com"
            or source.username or source.password or source.port not in (None, 443)
            or not source.path.startswith("/bangumi/Archive/releases/download/")):
        raise ValueError("Manifest source must be an official Bangumi Archive release URL")
    counts = manifest.get("counts")
    if not isinstance(counts, dict) or not counts:
        raise ValueError("Manifest is missing TSV row counts")
    for name, count in counts.items():
        if (not re.fullmatch(r"[A-Za-z0-9_-]+\.tsv", name)
                or type(count) is not int or count < 0):
            raise ValueError("Invalid manifest TSV row count")
    return manifest


class GitHubAPI:
    """A small REST client that does not forward credentials through redirects."""

    def __init__(self, repo, token, session=None, sleep=time.sleep):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
            raise ValueError("--repo must be owner/repository")
        if not token:
            raise ValueError("Set GH_TOKEN or GITHUB_TOKEN with contents:write permission")
        self.repo = repo
        self.token = token
        self.session = session or requests.Session()
        # Do not let ~/.netrc add unrelated credentials to a signed asset URL.
        self.session.trust_env = False
        self.sleep = sleep
        self.base = "https://api.github.com/repos/" + repo

    def request(self, method, url, *, accept="application/vnd.github+json", **kwargs):
        parsed = urlparse(url)
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443)):
            raise ValueError("GitHub requests require an HTTPS URL without credentials")
        is_api = parsed.hostname in API_HOSTS
        is_asset = parsed.hostname == "github.com" or (
            parsed.hostname and parsed.hostname.endswith(".githubusercontent.com"))
        if not is_api and not (method == "GET" and is_asset):
            raise ValueError("Refusing an unexpected GitHub API or asset host")
        headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "KBgm-publisher"}
        if is_api:
            headers["Authorization"] = "Bearer " + self.token
        headers.update(kwargs.pop("headers", {}))
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method, url, headers=headers, timeout=(15, 300),
                    allow_redirects=False, **kwargs)
            except requests.RequestException:
                if attempt + 1 == attempts:
                    raise RuntimeError("GitHub request failed (" + method + ")") from None
                self.sleep(2 ** attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < attempts:
                    response.close()
                    self.sleep(2 ** attempt)
                    continue
            return response
        raise AssertionError("Unreachable")

    def json_request(self, method, path, **kwargs):
        response = self.request(method, self.base + path, **kwargs)
        try:
            if not 200 <= response.status_code < 300:
                raise RuntimeError("GitHub %s %s returned HTTP %s" % (
                    method, path.split("?")[0], response.status_code))
            if response.status_code == 204:
                return None
            return strict_json(response.content)
        finally:
            response.close()

    def paginate(self, path):
        results = []
        for page in range(1, 1001):
            batch = self.json_request("GET", path, params={"per_page": 100, "page": page})
            if not isinstance(batch, list):
                raise ValueError("GitHub returned a non-list response")
            results.extend(batch)
            if len(batch) < 100:
                return results
        raise RuntimeError("GitHub pagination exceeded its safety limit")

    def releases(self):
        return self.paginate("/releases")

    def release(self, release_id):
        return self.json_request("GET", "/releases/%d" % release_id)

    def assets(self, release_id):
        return self.paginate("/releases/%d/assets" % release_id)

    def manifest(self, asset):
        if (not positive_integer(asset.get("id")) or not positive_integer(asset.get("size"))
                or asset["size"] > MAX_MANIFEST_BYTES):
            raise ValueError("Invalid or oversized release manifest asset")
        url = self.base + "/releases/assets/%d" % asset["id"]
        for _ in range(6):
            response = self.request("GET", url, accept="application/octet-stream", stream=True)
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        raise RuntimeError("GitHub asset redirect has no Location")
                    url = urljoin(url, location)
                    continue
                if response.status_code != 200:
                    raise RuntimeError("Manifest download returned HTTP %s" % response.status_code)
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_MANIFEST_BYTES:
                        raise ValueError("Release manifest exceeds size limit")
                    chunks.append(chunk)
                if size != asset["size"]:
                    raise ValueError("Release manifest asset size mismatch")
                return strict_json(b"".join(chunks))
            finally:
                response.close()
        raise RuntimeError("Too many GitHub asset redirects")

    def create_draft(self, version):
        body = {"tag_name": version, "name": version, "draft": True, "prerelease": False,
                "body": MARKER + "\nIncremental Bangumi data for KikoPlay. See manifest.json for provenance and checksums."}
        sha = os.environ.get("GITHUB_SHA", "")
        if re.fullmatch(r"[0-9a-f]{40}", sha):
            body["target_commitish"] = sha
        return self.json_request("POST", "/releases", json=body)

    def clear_draft_asset(self, release_id, name):
        require_managed_draft(self.release(release_id))
        for asset in self.assets(release_id):
            if asset.get("name") == name:
                if not positive_integer(asset.get("id")):
                    raise ValueError("Invalid GitHub asset ID")
                self.json_request("DELETE", "/releases/assets/%d" % asset["id"])

    def upload(self, release_id, path):
        path = Path(path)
        if path.name not in ASSET_NAMES:
            raise ValueError("Unexpected release asset name")
        # A failed upload can leave a 'starter' asset with the same name. Only
        # remove assets from our still-unpublished draft before retrying.
        for attempt in range(3):
            self.clear_draft_asset(release_id, path.name)
            response = None
            try:
                with path.open("rb") as stream:
                    response = self.request(
                        "POST", "https://uploads.github.com/repos/%s/releases/%d/assets" % (
                            self.repo, release_id), params={"name": path.name}, data=stream,
                        headers={"Content-Type": "application/zip" if path.suffix == ".zip"
                                 else "application/json"})
                if response.status_code == 201:
                    asset = strict_json(response.content)
                    verify_asset(asset, path.name, path.stat().st_size, file_sha256(path))
                    return asset
                if response.status_code not in (422, 429) and response.status_code < 500:
                    raise ValueError("Asset upload returned HTTP %s" % response.status_code)
            except RuntimeError:
                if attempt == 2:
                    raise
            finally:
                if response is not None:
                    response.close()
            if attempt < 2:
                self.sleep(2 ** attempt)
        raise RuntimeError("Asset upload failed after three attempts; draft retained for retry")

    def publish(self, release_id):
        require_managed_draft(self.release(release_id))
        return self.json_request("PATCH", "/releases/%d" % release_id,
                                 json={"draft": False, "make_latest": "true"})


def require_managed_draft(release, version=None):
    if (not isinstance(release, dict) or not positive_integer(release.get("id"))
            or release.get("draft") is not True or release.get("prerelease") is not False
            or MARKER not in (release.get("body") or "")):
        raise ValueError("Refusing to modify a release that is not a KBgm publisher draft")
    if version is not None and release.get("tag_name") != version:
        raise ValueError("Draft tag does not match the dump version")


def verify_asset(asset, name, size, sha256=None):
    if (not isinstance(asset, dict) or not positive_integer(asset.get("id"))
            or asset.get("name") != name or asset.get("state") != "uploaded"
            or type(asset.get("size")) is not int or asset["size"] != size):
        raise ValueError("Invalid or incomplete release asset: " + name)
    digest = asset.get("digest")
    if sha256 and digest is not None and digest != "sha256:" + sha256:
        raise ValueError("Release asset SHA-256 mismatch: " + name)


def published_history(api, releases):
    published = []
    for release in releases:
        if not isinstance(release, dict):
            raise ValueError("Malformed GitHub release")
        tag = release.get("tag_name")
        if not isinstance(tag, str) or not tag.startswith("dump-"):
            continue
        if release.get("draft") is True or release.get("prerelease") is True:
            continue
        if (release.get("draft") is not False or release.get("prerelease") is not False
                or not positive_integer(release.get("id"))):
            raise ValueError("Invalid published release metadata")
        version_key(tag)
        assets = api.assets(release["id"])
        manifests = [asset for asset in assets if asset.get("name") == "manifest.json"]
        archives = [asset for asset in assets if asset.get("name") == "bgm_extracted.zip"]
        if len(manifests) != 1 or len(archives) != 1:
            raise ValueError("Published dump must have exactly one manifest and one archive: " + tag)
        verify_asset(manifests[0], "manifest.json", manifests[0].get("size"))
        manifest = validate_manifest(api.manifest(manifests[0]), tag)
        archive = manifest["archive"]
        verify_asset(archives[0], archive["name"], archive["size"], archive["sha256"])
        published.append(manifest)
    published.sort(key=lambda manifest: version_key(manifest["dump_version"]))
    previous = None
    for manifest in published:
        if manifest["previous_version"] != previous:
            raise ValueError("Published manifest chain is incomplete or inconsistent at "
                             + manifest["dump_version"])
        previous = manifest["dump_version"]
    return published


def write_latest(path, manifest):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(manifest, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def run_pipeline(api, config, output_dir, since_override=None, *, latest_path=None,
                 fetcher=None, builder=None):
    if not isinstance(config, dict):
        raise ValueError("config.json must contain an object")
    bootstrap = iso_date(config.get("bootstrap_since"))
    buffer_days = config.get("buffer_days")
    if type(buffer_days) is not int or not 0 <= buffer_days <= 3660:
        raise ValueError("buffer_days must be an integer between 0 and 3660")
    requested_since = iso_date(since_override) if since_override is not None else None
    latest_path = Path(latest_path) if latest_path is not None else ROOT / "data/latest.json"
    releases = api.releases()
    history = published_history(api, releases)
    previous = history[-1] if history else None
    info = (fetcher or fetch_latest)()
    if not isinstance(info, dict) or not isinstance(info.get("name"), str) or not info["name"].endswith(".zip"):
        raise ValueError("Upstream metadata has no valid ZIP filename")
    version = info["name"][:-4]
    dump_date = parse_dump_version(version)
    if previous and version_key(version) < version_key(previous["dump_version"]):
        raise ValueError("Upstream dump is older than the latest published dump; refusing rollback")
    if previous and version == previous["dump_version"]:
        if requested_since and requested_since != iso_date(previous["since"]):
            raise ValueError("This dump is already published and immutable; --since cannot change it")
        write_latest(latest_path, previous)
        print("Already published; restored data/latest.json from release " + version)
        return previous
    baseline = (parse_dump_version(previous["dump_version"]) - dt.timedelta(days=buffer_days)
                if previous else bootstrap)
    if requested_since is not None and requested_since > baseline:
        raise ValueError("--since may only expand the window (must be <= %s)" % baseline)
    since = requested_since if requested_since is not None else baseline
    if since > dump_date:
        raise ValueError("The extraction start date is later than the upstream dump date")
    existing = [release for release in releases if release.get("tag_name") == version]
    if len(existing) > 1:
        raise ValueError("Multiple releases have the same dump tag")
    draft = existing[0] if existing else None
    if draft is not None:
        require_managed_draft(draft, version)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    previous_version = previous["dump_version"] if previous else None
    manifest = (builder or build_package)(info, since.isoformat(), previous_version, output_dir)
    validate_manifest(manifest, version)
    if manifest["previous_version"] != previous_version or manifest["since"] != since.isoformat():
        raise ValueError("Builder produced an unexpected publication window or previous_version")
    archive_path = output_dir / "bgm_extracted.zip"
    manifest_path = output_dir / "manifest.json"
    if (archive_path.stat().st_size != manifest["archive"]["size"]
            or file_sha256(archive_path) != manifest["archive"]["sha256"]):
        raise ValueError("Built archive does not match its manifest")
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES or strict_json(manifest_path.read_bytes()) != manifest:
        raise ValueError("Built manifest.json does not match the returned manifest")
    if draft is None:
        draft = api.create_draft(version)
    require_managed_draft(draft, version)
    release_id = draft["id"]
    api.upload(release_id, archive_path)
    api.upload(release_id, manifest_path)
    # Verify the remotely stored manifest and asset metadata before publication.
    pending_assets = api.assets(release_id)
    pending_manifests = [asset for asset in pending_assets if asset.get("name") == "manifest.json"]
    pending_archives = [asset for asset in pending_assets if asset.get("name") == "bgm_extracted.zip"]
    if len(pending_manifests) != 1 or len(pending_archives) != 1:
        raise ValueError("Draft is missing an uploaded manifest or archive")
    verify_asset(pending_archives[0], "bgm_extracted.zip", manifest["archive"]["size"],
                 manifest["archive"]["sha256"])
    if api.manifest(pending_manifests[0]) != manifest:
        raise ValueError("Remote draft manifest does not match the built package")
    published = api.publish(release_id)
    if (published.get("draft") is not False or published.get("prerelease") is not False
            or published.get("tag_name") != version or published.get("id") != release_id):
        raise ValueError("GitHub did not confirm the expected release publication")
    write_latest(latest_path, manifest)
    print("Published " + version + "; updated data/latest.json")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--since", help="YYYY-MM-DD; only expand the normal extraction window")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args(argv)
    try:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        api = GitHubAPI(args.repo, token)
        run_pipeline(api, strict_json(args.config.read_bytes()), args.output, args.since)
    except (OSError, ValueError, RuntimeError, requests.RequestException) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
