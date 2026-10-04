import csv
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import warnings
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_dump


VERSION = "dump-2026-10-06.210001Z"
PREVIOUS_VERSION = "dump-2026-09-29.210001Z"


def metadata(data=b"unused"):
    return {
        "name": VERSION + ".zip",
        "browser_download_url": "https://github.com/bangumi/Archive/releases/download/archive/" + VERSION + ".zip",
        "size": len(data),
        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
    }


def make_archive(path, extra_members=()):
    subjects = [
        {"id": 1, "type": 2, "name": "Original", "name_cn": "边界动画", "date": "2026-07-01",
         "summary": 'Line 1\nLine 2\t"quote"\\path', "meta_tags": ["冒险", "TV", "2026", "原创"],
         "infobox": "{{Infobox animanga/TVAnime\n|话数=12\n|别名={\n[别称]\n}\n|官方网站=https://example.com\n|导演=导演甲\n}}"},
        {"id": 2, "type": 2, "name_cn": "窗口之前", "date": "2026-06-30"},
        {"id": 3, "type": 2, "name_cn": "未来动画", "date": "2027-01-01"},
        {"id": 4, "type": 1, "name_cn": "其他类型", "date": "2026-07-01"},
        {"id": 5, "type": 2, "name_cn": "无效日期", "date": "2026-02-30"},
    ]
    episodes = [
        {"subject_id": 1, "type": 0, "sort": 1, "name_cn": "第一集"},
        {"subject_id": 1, "type": 1, "sort": 1.5, "name": "Special"},
        {"subject_id": 1, "type": 2, "sort": 1, "name": "Unsupported"},
        {"subject_id": 2, "type": 0, "sort": 1},
        {"subject_id": 3, "type": 0, "sort": 1},
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("dump/subject.jsonlines", "\n".join(json.dumps(x, ensure_ascii=False) for x in subjects))
            archive.writestr("dump/episode.jsonlines", "\n".join(json.dumps(x, ensure_ascii=False) for x in episodes))
            archive.writestr("dump/unused.jsonlines", "This must not be extracted")
            for name, value in extra_members:
                archive.writestr(name, value)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.zip"
        make_archive(self.source)
        self.output = self.root / "output"
        self.info = metadata(self.source.read_bytes())

    def fake_download(self, url, destination, size, digest):
        shutil.copyfile(self.source, destination)
        return build_dump._sha256(destination), destination.stat().st_size

    def build(self):
        with mock.patch.object(build_dump, "_download", side_effect=self.fake_download):
            return build_dump.build_package(self.info, "2026-07-01", PREVIOUS_VERSION, self.output)

    def assert_no_output(self):
        self.assertFalse((self.output / "manifest.json").exists())
        self.assertFalse((self.output / "bgm_extracted.zip").exists())
        self.assertEqual(list(self.output.glob(".kbgm-build-*")), [])

    def test_version_validation(self):
        self.assertEqual(build_dump.parse_dump_version(VERSION), dt.date(2026, 10, 6))
        for value in ("dump-2026-10-06", "dump-2026-10-06.210001Z.zip", "dump-2026-02-30.010000Z",
                      "dump-2026-10-06.246001Z", "dump-2026-1-6.210001Z", VERSION + "\n", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                build_dump.parse_dump_version(value)

    def test_inclusive_window_compatible_tsv_and_manifest(self):
        manifest = self.build()
        self.assertEqual(manifest, json.loads((self.output / "manifest.json").read_text(encoding="utf-8")))
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["previous_version"], PREVIOUS_VERSION)
        self.assertEqual(manifest["since"], "2026-07-01")
        self.assertIsNotNone(dt.datetime.fromisoformat(manifest["generated_at"]).tzinfo)
        package = self.output / "bgm_extracted.zip"
        self.assertEqual(manifest["archive"]["sha256"], hashlib.sha256(package.read_bytes()).hexdigest())
        self.assertEqual(manifest["archive"]["size"], package.stat().st_size)
        self.assertEqual(manifest["source"]["sha256"], build_dump._sha256(self.source))
        self.assertEqual(manifest["source"]["size"], self.source.stat().st_size)
        with zipfile.ZipFile(package) as archive:
            self.assertEqual(set(archive.namelist()), set(build_dump.TSV_FILES) | {"dump_version.txt"})
            self.assertEqual(archive.read("dump_version.txt").decode(), VERSION + "\n")
            rows = {name: list(csv.reader(io.StringIO(archive.read(name).decode("utf-8"), newline=""),
                                         delimiter="\t", escapechar="\\")) for name in build_dump.TSV_FILES}
        self.assertEqual(manifest["counts"], {name: len(value) for name, value in rows.items()})
        self.assertEqual(manifest["counts"], {"anime_profile.tsv": 2, "anime_info.tsv": 2,
                         "anime_source.tsv": 2, "anime_tag.tsv": 2, "pool_info.tsv": 3})
        for name, width in zip(build_dump.TSV_FILES, (11, 3, 3, 2, 6)):
            self.assertTrue(all(len(row) == width for row in rows[name]))
        self.assertEqual([row[0] for row in rows["anime_profile.tsv"]], ["1", "3"])
        profile = rows["anime_profile.tsv"][0]
        self.assertEqual(profile[4], 'Line 1\nLine 2\t"quote"\\path')
        self.assertEqual(profile[9], "12")
        self.assertEqual(json.loads(profile[7])["导演"], "导演甲")
        self.assertEqual(json.loads(profile[10]), ["别称"])
        nameid = hashlib.md5("边界动画".encode()).hexdigest()
        self.assertEqual(rows["anime_info.tsv"][0][0], nameid)
        self.assertEqual(rows["pool_info.tsv"][0][0], hashlib.md5("边界动画 1.1".encode()).hexdigest())
        self.assertEqual(rows["pool_info.tsv"][1][3:5], ["2", "1.5"])

    def test_only_required_inputs_are_extracted(self):
        target = self.root / "extracted"
        build_dump._extract_inputs(self.source, target)
        self.assertEqual({path.name for path in target.iterdir()}, set(build_dump.INPUT_FILES))

    def test_unsafe_and_duplicate_members_fail_without_publishing(self):
        for name in ("../escape", "/absolute", "C:/absolute", "dump\\subject.jsonlines", "dump//extra",
                     "dump/subject.jsonlines", "other/subject.jsonlines"):
            with self.subTest(name=name):
                make_archive(self.source, [(name, "unsafe")])
                with self.assertRaises(ValueError):
                    self.build()
                self.assert_no_output()

    def test_symlink_is_rejected(self):
        member = zipfile.ZipInfo("symlink")
        member.create_system = 3
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        make_archive(self.source, [(member, "../target")])
        with self.assertRaises(ValueError):
            self.build()
        self.assert_no_output()

    def test_missing_input_is_rejected(self):
        with zipfile.ZipFile(self.source, "w") as archive:
            archive.writestr("subject.jsonlines", "")
        with self.assertRaises(ValueError):
            self.build()
        self.assert_no_output()

    def test_extraction_failure_does_not_publish_manifest(self):
        with mock.patch.object(build_dump.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaisesRegex(RuntimeError, "extraction failed"):
                self.build()
        self.assert_no_output()

    def test_invalid_since_and_previous_are_rejected(self):
        for since, previous in (("2026-7-1", None), ("2026-02-30", None), ("2026-10-07", None),
                                ("2026-07-01", VERSION), ("2026-07-01", "not-a-version")):
            with self.subTest(since=since, previous=previous), self.assertRaises(ValueError):
                build_dump.build_package(self.info, since, previous, self.output)
        self.assert_no_output()

    def test_first_package_previous_is_null(self):
        with mock.patch.object(build_dump, "_download", side_effect=self.fake_download):
            manifest = build_dump.build_package(self.info, "2026-07-01", None, self.output)
        self.assertIsNone(manifest["previous_version"])

    def test_missing_wiki_dependency_is_fatal_even_for_empty_input(self):
        # -S excludes site-packages; clearing PYTHONPATH also excludes task-local dependencies.
        import os
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        result = subprocess.run([sys.executable, "-S", str(Path(build_dump.__file__).with_name("extract_local.py")),
                                 "--help"], env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bgm_tv_wiki", result.stderr)


class DownloadTests(unittest.TestCase):
    def response(self, data):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status_code = 200
        response.iter_content.return_value = [data]
        return response

    def test_download_integrity_and_cleanup_on_failure(self):
        data = b"test upstream bytes"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for size, digest in ((len(data), "0" * 64), (len(data) + 1, None), (len(data) - 1, None)):
                with self.subTest(size=size, digest=digest):
                    info = metadata(data)
                    info["size"] = size
                    info["digest"] = None if digest is None else "sha256:" + digest
                    with mock.patch.object(build_dump, "_session") as factory:
                        factory.return_value.__enter__.return_value.get.return_value = self.response(data)
                        with self.assertRaises(RuntimeError):
                            build_dump.build_package(info, "2026-07-01", None, root / "output")
                    self.assertEqual(list((root / "output").iterdir()), [])

    def test_download_restarts_interrupted_stream(self):
        data = b"complete"
        interrupted = self.response(data)
        interrupted.iter_content.side_effect = build_dump.requests.ConnectionError("secret URL should not escape")
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(build_dump, "_session") as factory:
            session = factory.return_value.__enter__.return_value
            session.get.side_effect = [interrupted, self.response(data)]
            path = Path(temporary) / "download.zip"
            with mock.patch.object(build_dump.time, "sleep"):
                digest, size = build_dump._download("https://example.invalid", path, len(data), None)
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(digest, hashlib.sha256(data).hexdigest())
            self.assertEqual(size, len(data))
            self.assertEqual(session.get.call_count, 2)

    def test_metadata_rejects_unsafe_sources(self):
        for field, value in (("browser_download_url", "http://github.com/bangumi/Archive/releases/download/x/" + VERSION + ".zip"),
                             ("browser_download_url", "https://other.invalid/dump.zip"),
                             ("name", "../dump.zip"), ("size", True), ("size", 0), ("digest", "md5:invalid")):
            info = metadata()
            info[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                build_dump._validate_metadata(info)


if __name__ == "__main__":
    unittest.main()
