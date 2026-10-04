#!/usr/bin/env python3
"""Build the KikoPlay-compatible package without connecting to any server."""

import argparse
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import zipfile

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


LATEST_JSON_URL = (
    "https://raw.githubusercontent.com/bangumi/Archive/refs/heads/master/aux/latest.json"
)
TSV_FILES = (
    "anime_profile.tsv", "anime_info.tsv", "anime_source.tsv",
    "anime_tag.tsv", "pool_info.tsv",
)
INPUT_FILES = ("subject.jsonlines", "episode.jsonlines")
MAX_EXTRACTED_BYTES = 8 * 1024 ** 3
_VERSION_RE = re.compile(r"dump-[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]{6}Z")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")


def parse_dump_version(version):
    """Validate the complete timestamp and return its UTC calendar date."""
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        raise ValueError("Invalid dump version; expected dump-YYYY-MM-DD.HHMMSSZ")
    return dt.datetime.strptime(version, "dump-%Y-%m-%d.%H%M%SZ").date()


def _parse_since(since):
    if not isinstance(since, str) or not _DATE_RE.fullmatch(since):
        raise ValueError("Invalid since date; expected YYYY-MM-DD")
    return dt.date.fromisoformat(since)


def _validate_metadata(info):
    if not isinstance(info, dict):
        raise ValueError("Upstream metadata must be an object")
    name = info.get("name")
    if not isinstance(name, str) or not name.endswith(".zip"):
        raise ValueError("Upstream asset must be a versioned ZIP")
    version = name[:-4]
    parse_dump_version(version)
    url = info.get("browser_download_url")
    if not isinstance(url, str):
        raise ValueError("Missing upstream download URL")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "github.com"
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not parsed.path.startswith("/bangumi/Archive/releases/download/")
            or parsed.path.rsplit("/", 1)[-1] != name):
        raise ValueError("Unexpected upstream download URL")
    size = info.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError("Missing or invalid upstream asset size")
    digest = info.get("digest")
    if digest is not None:
        if (not isinstance(digest, str) or not digest.startswith("sha256:")
                or not _SHA256_RE.fullmatch(digest[7:])):
            raise ValueError("Unsupported or invalid upstream digest")
        digest = digest[7:].lower()
    return version, url, size, digest


def _session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset({"GET"}))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "KBgm-dump-producer/1", "Accept-Encoding": "identity"})
    return session


def fetch_latest():
    """Read and validate Bangumi's published latest.json asset metadata."""
    try:
        with _session() as session:
            with session.get(LATEST_JSON_URL, timeout=(15, 60)) as response:
                response.raise_for_status()
                info = response.json()
    except (requests.RequestException, ValueError):
        raise RuntimeError("Unable to fetch upstream metadata") from None
    _validate_metadata(info)
    return info


def _download(url, destination, expected_size, expected_digest):
    """Restart interrupted streams; never reuse an incomplete download."""
    with _session() as session:
        for attempt in range(3):
            digest = hashlib.sha256()
            size = 0
            try:
                with session.get(url, stream=True, timeout=(15, 120)) as response:
                    response.raise_for_status()
                    if response.status_code != 200:
                        raise RuntimeError("Upstream did not return a complete asset")
                    with destination.open("wb") as output:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            size += len(chunk)
                            if size > expected_size:
                                raise RuntimeError("Upstream asset exceeds its declared size")
                            digest.update(chunk)
                            output.write(chunk)
                break
            except requests.RequestException:
                if attempt == 2:
                    raise RuntimeError("Upstream asset download failed after retries") from None
                time.sleep(2 ** attempt)
    if size != expected_size:
        raise RuntimeError("Upstream asset size does not match metadata")
    actual_digest = digest.hexdigest()
    if expected_digest and actual_digest != expected_digest:
        raise RuntimeError("Upstream asset SHA-256 does not match metadata")
    return actual_digest, size


def _extract_inputs(archive_path, destination):
    """Inspect all names, then stream only the two required inputs to fixed paths."""
    selected = {}
    with zipfile.ZipFile(archive_path) as archive:
        names = set()
        for member in archive.infolist():
            name = member.filename
            parts = PurePosixPath(name).parts
            if (not name or name.startswith("/") or "\\" in name or ":" in name
                    or "\x00" in name or any(part in (".", "..", "") for part in name.rstrip("/").split("/"))):
                raise ValueError("Unsafe ZIP member path")
            if name in names:
                raise ValueError("Duplicate ZIP member path")
            names.add(name)
            mode = stat.S_IFMT(member.external_attr >> 16)
            if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError("ZIP contains a non-regular member")
            if member.is_dir():
                continue
            basename = parts[-1]
            if basename not in INPUT_FILES:
                continue
            if basename in selected:
                raise ValueError("ZIP contains duplicate required inputs")
            if member.flag_bits & 1:
                raise ValueError("Encrypted ZIP inputs are unsupported")
            selected[basename] = member
        if set(selected) != set(INPUT_FILES):
            raise ValueError("ZIP is missing subject.jsonlines or episode.jsonlines")
        if len({PurePosixPath(member.filename).parent for member in selected.values()}) != 1:
            raise ValueError("ZIP inputs must share a directory")
        if sum(member.file_size for member in selected.values()) > MAX_EXTRACTED_BYTES:
            raise ValueError("ZIP inputs exceed the extraction size limit")
        destination.mkdir()
        for basename, member in selected.items():
            with archive.open(member) as source, (destination / basename).open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _count_rows(path):
    # A quoted summary may contain newlines: count TSV records, not physical lines.
    csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
    with path.open(encoding="utf-8", newline="") as source:
        return sum(1 for _ in csv.reader(source, delimiter="\t", escapechar="\\"))


def build_package(info, since, previous_version, output_dir):
    """Build a fresh output directory; only a complete package receives a manifest."""
    version, url, expected_size, expected_digest = _validate_metadata(info)
    since_date = _parse_since(since)
    if since_date > parse_dump_version(version):
        raise ValueError("The since date must not be later than the dump date")
    if previous_version is not None:
        parse_dump_version(previous_version)
        if previous_version >= version:
            raise ValueError("Previous version must precede the current dump")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    final_archive = output_dir / "bgm_extracted.zip"
    final_manifest = output_dir / "manifest.json"
    if final_archive.exists() or final_manifest.exists():
        raise ValueError("Output package already exists; choose a fresh output directory")
    with tempfile.TemporaryDirectory(prefix=".kbgm-build-", dir=output_dir) as temporary:
        work = Path(temporary)
        source_archive = work / "source.zip"
        source_sha256, source_size = _download(url, source_archive, expected_size, expected_digest)
        dump_dir = work / "inputs"
        _extract_inputs(source_archive, dump_dir)
        extracted_dir = work / "extracted"
        result = subprocess.run([
            sys.executable, str(Path(__file__).with_name("extract_local.py")),
            str(dump_dir), str(extracted_dir), "--since", since,
        ], check=False)
        if result.returncode:
            raise RuntimeError("TSV extraction failed")
        counts = {name: _count_rows(extracted_dir / name) for name in TSV_FILES}
        package = work / "bgm_extracted.zip"
        with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name in TSV_FILES:
                archive.write(extracted_dir / name, arcname=name)
            archive.writestr("dump_version.txt", version + "\n")
        manifest = {
            "schema_version": 1,
            "dump_version": version,
            "previous_version": previous_version,
            "since": since,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "archive": {"name": package.name, "sha256": _sha256(package), "size": package.stat().st_size},
            "source": {"name": info["name"], "url": url, "sha256": source_sha256, "size": source_size},
            "counts": counts,
        }
        manifest_path = work / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        package.replace(final_archive)
        try:
            manifest_path.replace(final_manifest)
        except OSError:
            final_archive.unlink(missing_ok=True)
            raise
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, help="Saved upstream latest.json (otherwise fetched live)")
    parser.add_argument("--since", required=True, help="Inclusive first air date, YYYY-MM-DD")
    parser.add_argument("--previous-version", help="Previous successfully published dump version")
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    try:
        info = json.loads(args.metadata.read_text(encoding="utf-8")) if args.metadata else fetch_latest()
        manifest = build_package(info, args.since, args.previous_version, args.output)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
        # Network exceptions are sanitized at their source, without printing URLs/tokens.
        print("Build failed: {}".format(error), file=sys.stderr)
        return 1
    print("Built {} ({} bytes)".format(manifest["dump_version"], manifest["archive"]["size"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
