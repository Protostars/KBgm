"""Publication/state recovery tests; these tests never access the network."""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import publish_dump as publisher


V1 = "dump-2026-09-22.210000Z"
V2 = "dump-2026-09-29.210000Z"
V3 = "dump-2026-10-06.210000Z"
CONFIG = {"bootstrap_since": "2026-07-01", "buffer_days": 90}
ARCHIVE = b"test archive payload"


def manifest(version=V2, previous=None, since="2026-07-01"):
    return {
        "schema_version": 1, "dump_version": version, "previous_version": previous,
        "since": since, "generated_at": "2026-10-04T00:00:00Z",
        "archive": {"name": "bgm_extracted.zip", "size": len(ARCHIVE),
                    "sha256": hashlib.sha256(ARCHIVE).hexdigest()},
        "source": {"name": version + ".zip", "size": 123456, "sha256": "a" * 64,
                   "url": "https://github.com/bangumi/Archive/releases/download/" + version + "/" + version + ".zip"},
        "counts": {"anime_profile.tsv": 1},
    }


def asset(name, data, asset_id):
    return {"id": asset_id, "name": name, "state": "uploaded", "size": len(data),
            "digest": "sha256:" + hashlib.sha256(data).hexdigest()}


class FakeAPI:
    def __init__(self):
        self.items = []
        self.asset_data = {}
        self.next_id = 1
        self.upload_calls = []
        self.publish_calls = []
        self.create_calls = []
        self.fail_upload = None
        self.fail_publish = False

    def seed(self, data, draft=False, managed=True, prerelease=False):
        release_id = len(self.items) + 1
        item = {"id": release_id, "tag_name": data["dump_version"], "draft": draft,
                "prerelease": prerelease, "body": publisher.MARKER if managed else "manual",
                "assets": []}
        self.items.append(item)
        for name, contents in (("bgm_extracted.zip", ARCHIVE),
                               ("manifest.json", json.dumps(data).encode())):
            metadata = asset(name, contents, self.next_id)
            self.next_id += 1
            item["assets"].append(metadata)
            self.asset_data[metadata["id"]] = contents
        return item

    def releases(self):
        # created_at/API order must not determine publication history.
        return copy.deepcopy(list(reversed(self.items)))

    def release(self, release_id):
        return next(item for item in self.items if item["id"] == release_id)

    def assets(self, release_id):
        return copy.deepcopy(self.release(release_id)["assets"])

    def manifest(self, metadata):
        return json.loads(self.asset_data[metadata["id"]])

    def create_draft(self, version):
        self.create_calls.append(version)
        item = {"id": len(self.items) + 1, "tag_name": version, "draft": True,
                "prerelease": False, "body": publisher.MARKER, "assets": []}
        self.items.append(item)
        return copy.deepcopy(item)

    def upload(self, release_id, path):
        self.upload_calls.append(path.name)
        if self.fail_upload == path.name:
            raise RuntimeError("Simulated upload failure")
        item = self.release(release_id)
        publisher.require_managed_draft(item)
        item["assets"] = [a for a in item["assets"] if a["name"] != path.name]
        data = path.read_bytes()
        metadata = asset(path.name, data, self.next_id)
        self.next_id += 1
        item["assets"].append(metadata)
        self.asset_data[metadata["id"]] = data
        return metadata

    def publish(self, release_id):
        self.publish_calls.append(release_id)
        if self.fail_publish:
            raise RuntimeError("Simulated publication failure")
        item = self.release(release_id)
        item["draft"] = False
        return copy.deepcopy(item)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.latest = self.directory / "data/latest.json"
        self.output = self.directory / "dist"
        self.api = FakeAPI()
        self.builder = Mock(side_effect=self.build)

    def build(self, info, since, previous_version, output_dir):
        data = manifest(info["name"][:-4], previous_version, since)
        (output_dir / "bgm_extracted.zip").write_bytes(ARCHIVE)
        (output_dir / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
        return data

    def run_pipeline(self, version=V2, since=None):
        return publisher.run_pipeline(
            self.api, CONFIG, self.output, since, latest_path=self.latest,
            fetcher=lambda: {"name": version + ".zip"}, builder=self.builder)

    def test_first_publication_writes_state_after_both_assets_publish(self):
        result = self.run_pipeline()
        self.assertEqual(result["since"], "2026-07-01")
        self.assertIsNone(result["previous_version"])
        self.assertEqual(self.api.upload_calls, ["bgm_extracted.zip", "manifest.json"])
        self.assertEqual(self.api.publish_calls, [1])
        self.assertEqual(json.loads(self.latest.read_text()), result)
        self.assertFalse(self.api.items[0]["draft"])

    def test_recover_same_version_from_published_release_without_rebuilding(self):
        expected = manifest()
        self.api.seed(expected)
        self.latest.parent.mkdir()
        self.latest.write_text('{"stale": true}')
        self.assertEqual(self.run_pipeline(), expected)
        self.assertEqual(json.loads(self.latest.read_text()), expected)
        self.builder.assert_not_called()
        self.assertEqual(self.api.upload_calls, [])

    def test_next_publication_uses_release_history_not_stale_repo_cursor(self):
        self.api.seed(manifest(V1))
        self.api.seed(manifest(V2, V1, "2026-06-24"))
        self.latest.parent.mkdir()
        self.latest.write_text(json.dumps(manifest(V1)))
        result = self.run_pipeline(V3)
        self.assertEqual(result["previous_version"], V2)
        self.assertEqual(result["since"], "2026-07-01")

    def test_upload_failure_does_not_advance_state_or_publish(self):
        self.api.seed(manifest(V1))
        publisher.write_latest(self.latest, manifest(V1))
        before = self.latest.read_bytes()
        self.api.fail_upload = "manifest.json"
        with self.assertRaisesRegex(RuntimeError, "upload failure"):
            self.run_pipeline()
        self.assertEqual(self.latest.read_bytes(), before)
        self.assertEqual(self.api.publish_calls, [])
        self.assertTrue(self.api.items[-1]["draft"])

    def test_publication_failure_does_not_write_state(self):
        self.api.fail_publish = True
        with self.assertRaisesRegex(RuntimeError, "publication failure"):
            self.run_pipeline()
        self.assertFalse(self.latest.exists())

    def test_state_write_failure_after_publication_recovers_without_rebuilding(self):
        with patch.object(publisher, "write_latest", side_effect=OSError("disk error")):
            with self.assertRaisesRegex(OSError, "disk error"):
                self.run_pipeline()
        self.builder.reset_mock()
        self.run_pipeline()
        self.builder.assert_not_called()
        self.assertTrue(self.latest.exists())
        self.assertEqual(len(self.api.publish_calls), 1)

    def test_owned_draft_is_reused_and_assets_replaced(self):
        draft = self.api.seed(manifest(), draft=True)
        old_asset_ids = {a["id"] for a in draft["assets"]}
        self.run_pipeline()
        self.assertEqual(self.api.create_calls, [])
        self.assertEqual(self.api.publish_calls, [draft["id"]])
        self.assertTrue(old_asset_ids.isdisjoint({a["id"] for a in draft["assets"]}))

    def test_unowned_draft_is_never_modified(self):
        self.api.seed(manifest(), draft=True, managed=False)
        with self.assertRaisesRegex(ValueError, "not a KBgm publisher draft"):
            self.run_pipeline()
        self.builder.assert_not_called()
        self.assertEqual(self.api.upload_calls, [])

    def test_manual_since_may_expand_but_not_shrink(self):
        with self.assertRaisesRegex(ValueError, "only expand"):
            self.run_pipeline(since="2026-07-02")
        self.builder.assert_not_called()
        self.assertEqual(self.run_pipeline(since="2026-01-01")["since"], "2026-01-01")

    def test_same_published_version_cannot_be_rebuilt_with_new_since(self):
        self.api.seed(manifest())
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.run_pipeline(since="2026-01-01")
        self.builder.assert_not_called()

    def test_rollback_is_rejected(self):
        self.api.seed(manifest(V2))
        with self.assertRaisesRegex(ValueError, "refusing rollback"):
            self.run_pipeline(V1)
        self.builder.assert_not_called()

    def test_missing_chain_predecessor_is_rejected(self):
        self.api.seed(manifest(V2, V1))
        with self.assertRaisesRegex(ValueError, "chain is incomplete"):
            self.run_pipeline(V3)
        self.builder.assert_not_called()

    def test_archive_hash_in_release_metadata_is_checked(self):
        item = self.api.seed(manifest())
        item["assets"][0]["digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            self.run_pipeline()

    def test_incomplete_published_release_is_rejected(self):
        item = self.api.seed(manifest())
        item["assets"] = item["assets"][:1]
        with self.assertRaisesRegex(ValueError, "exactly one manifest"):
            self.run_pipeline()

    def test_other_prereleases_do_not_advance_publication_window(self):
        self.api.seed(manifest(V3), prerelease=True)
        self.assertEqual(self.run_pipeline(V2)["since"], "2026-07-01")


def response(status, content=b"", headers=None):
    value = Mock(status_code=status, content=content, headers=headers or {})
    value.iter_content.return_value = [content]
    return value


class GitHubAPITests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.api = publisher.GitHubAPI("Protostars/KBgm", "private-token", self.session,
                                       sleep=lambda _: None)

    def test_asset_api_redirect_does_not_forward_token(self):
        data = json.dumps(manifest()).encode()
        self.session.request.side_effect = [
            response(302, headers={"Location": "https://release-assets.githubusercontent.com/signed"}),
            response(200, data),
        ]
        self.assertEqual(self.api.manifest({"id": 1, "size": len(data)}), manifest())
        calls = self.session.request.call_args_list
        self.assertIn("/releases/assets/1", calls[0].args[1])
        self.assertEqual(calls[0].kwargs["headers"]["Authorization"], "Bearer private-token")
        self.assertNotIn("Authorization", calls[1].kwargs["headers"])
        self.assertFalse(calls[0].kwargs["allow_redirects"])

    def test_untrusted_redirect_is_rejected_without_sending_request(self):
        self.session.request.return_value = response(
            302, headers={"Location": "https://attacker.example/data"})
        with self.assertRaisesRegex(ValueError, "unexpected GitHub"):
            self.api.manifest({"id": 1, "size": 123})
        self.assertEqual(self.session.request.call_count, 1)

    def test_asset_size_mismatch_is_rejected(self):
        self.session.request.return_value = response(200, b"{}")
        with self.assertRaisesRegex(ValueError, "size mismatch"):
            self.api.manifest({"id": 1, "size": 100})

    def test_read_retry_is_bounded_and_mutations_are_not_blindly_retried(self):
        self.session.request.side_effect = [response(503), response(200, b"[]")]
        self.assertEqual(self.api.releases(), [])
        self.assertEqual(self.session.request.call_count, 2)
        self.session.request.reset_mock()
        self.session.request.side_effect = [response(503)]
        with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
            self.api.create_draft(V2)
        self.assertEqual(self.session.request.call_count, 1)

    def test_upload_retries_clear_only_named_asset_of_managed_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bgm_extracted.zip"
            path.write_bytes(ARCHIVE)
            release = {"id": 7, "tag_name": V2, "draft": True, "prerelease": False,
                       "body": publisher.MARKER}
            starter = {"id": 50, "name": path.name, "state": "starter", "size": 0}
            self.api.release = Mock(return_value=release)
            self.api.assets = Mock(side_effect=[[], [starter]])
            self.api.json_request = Mock()
            self.session.request.side_effect = [response(502), response(201, json.dumps(
                asset(path.name, ARCHIVE, 51)).encode())]
            result = self.api.upload(7, path)
            self.assertEqual(result["id"], 51)
            self.api.json_request.assert_called_once_with("DELETE", "/releases/assets/50")

    def test_duplicate_json_keys_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate JSON"):
            publisher.strict_json('{"draft": true, "draft": false}')


if __name__ == "__main__":
    unittest.main()
