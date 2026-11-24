import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import retention  # noqa: E402

IMAGE = "ghcr.io/pipesor-org/pipesor-pipeline-service"
OTHER = "ghcr.io/pipesor-org/pipesor-rag-retrieval"
NOW = datetime(2026, 12, 31, tzinfo=timezone.utc)
INDEX = "application/vnd.oci.image.index.v1+json"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"


def digest(n):
    return f"sha256:{n:064x}"


def source(n):
    return f"{n:040x}"


def version(vid, days_old, tags, n=None):
    return {"id": vid, "name": digest(n if n is not None else vid), "created_at": (NOW - timedelta(days=days_old)).isoformat(),
            "metadata": {"container": {"tags": tags}}}


def technical(vid, days_old, n=None):
    return version(vid, days_old, [f"sha-{source(vid)}-{1000 + vid}-1"], n)


class FakeGitHub:
    def __init__(self, versions, manifests=None, releases=(), assets=None, tags=(), fail_delete=None):
        self.versions, self.manifests = versions, manifests or {}
        self.releases, self.assets, self.tags = list(releases), assets or {}, list(tags)
        self.deleted, self.fail_delete = [], fail_delete

    def paginate(self, path):
        if "/packages/container/" in path:
            return self.versions
        if path.endswith("/releases?per_page=100"):
            return self.releases
        if path.endswith("/tags?per_page=100"):
            return self.tags
        raise AssertionError(path)

    def api_get(self, path, accept=None):
        return json.dumps(self.assets[int(path.rsplit("/", 1)[1])]).encode()

    def manifest(self, owner, package, ref):
        if ref not in self.manifests and ref == "broken":
            raise retention.RetentionError("Manifest returned 500")
        return self.manifests.get(ref, {"mediaType": MANIFEST})

    def delete_version(self, owner, package, version_id, missing_ok=False):
        if version_id == self.fail_delete:
            raise retention.RetentionError("Deleting returned 403")
        self.deleted.append(version_id)


class IacHistory:
    """Temporary Git repository with a sequence of images.json commits."""

    def __init__(self, test):
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.git("init", "--quiet")

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)

    def commit(self, environment, images, schema=1):
        path = self.root / "environments" / environment / "images.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {"schema_version": schema, "environment": environment,
                    "images": {name: {"image": image, "digest": value} for name, (image, value) in images.items()}}
        path.write_text(json.dumps(document))
        self.git("add", "-A")
        self.git("commit", "--quiet", "--allow-empty", "-m", f"deploy {environment}")


class DeployedHistoryTests(unittest.TestCase):
    def test_current_and_recent_distinct_digests_per_environment(self):
        iac = IacHistory(self)
        for n in (1, 2, 2, 3, 4, 5):
            iac.commit("dev", {"pipeline": (IMAGE, digest(n)), "rag": (OTHER, digest(90))})
        iac.commit("qa", {"pipeline": (IMAGE, digest(3))})
        deployed = retention.deployed_digests(iac.root, IMAGE, keep_deployed=3)
        self.assertEqual([digest(5), digest(4), digest(3), digest(2)], deployed["dev"])
        self.assertEqual([digest(3)], deployed["qa"])
        self.assertNotIn(digest(90), deployed["dev"])

    def test_missing_history_and_unknown_schema_stop_the_run(self):
        empty = IacHistory(self)
        (empty.root / "README.md").write_text("x")
        empty.git("add", "-A")
        empty.git("commit", "--quiet", "-m", "init")
        with self.assertRaisesRegex(retention.RetentionError, "No environments"):
            retention.deployed_digests(empty.root, IMAGE, 3)
        future = IacHistory(self)
        future.commit("dev", {"pipeline": (IMAGE, digest(1))}, schema=2)
        with self.assertRaisesRegex(retention.RetentionError, "unknown schema"):
            retention.deployed_digests(future.root, IMAGE, 3)
        with self.assertRaisesRegex(retention.RetentionError, "Git checkout"):
            retention.deployed_digests(Path(tempfile.gettempdir()) / "pipesor-missing-iac", IMAGE, 3)


class PlanTests(unittest.TestCase):
    def plan(self, versions, **overrides):
        options = {"children": {}, "protected_digests": set(), "protected_sources": set(), "now": NOW,
                   "min_age_days": 30, "keep_recent": 2}
        options.update(overrides)
        keep, delete = retention.plan(retention.normalize(versions), **options)
        return {r["id"]: r["reasons"] for r in keep}, {r["id"]: r["reasons"] for r in delete}

    def test_each_rule_protects_and_old_unprotected_candidates_are_deleted(self):
        versions = [technical(1, 100), technical(2, 90), technical(3, 80), technical(4, 70), technical(5, 5),
                    technical(6, 3), version(7, 200, ["latest"]), version(8, 200, ["v1.0.0", f"sha-{source(8)}-9-1"]),
                    version(9, 120, [f"sha-{source(9)}"])]
        keep, delete = self.plan(versions, protected_digests={digest(1)}, protected_sources={source(2)})
        self.assertEqual(["protected_digest"], keep[1])
        self.assertEqual(["release_source"], keep[2])
        self.assertIn("newest_candidates", keep[6])
        self.assertIn("recent", keep[5])
        self.assertEqual(["non_technical_tags"], keep[7])
        self.assertEqual(["non_technical_tags"], keep[8])
        self.assertEqual({3: ["old_unprotected_candidate"], 4: ["old_unprotected_candidate"],
                          9: ["old_unprotected_candidate"]}, delete)

    def test_index_children_follow_their_parent_and_orphans_need_age(self):
        versions = [technical(1, 100), version(2, 100, [], n=2), version(3, 100, [], n=3),
                    technical(4, 100), version(5, 100, [], n=5), version(6, 100, [], n=6), version(7, 2, [], n=7),
                    version(8, 100, [], n=8)]
        children = {digest(2): {1}, digest(3): {1}, digest(5): {4}, digest(8): {1, 4}}
        keep, delete = self.plan(versions, children=children, protected_digests={digest(1)}, keep_recent=0)
        self.assertEqual(["child_of_kept_index"], keep[2])
        self.assertEqual(["child_of_kept_index"], keep[8])
        self.assertEqual(["recent_untagged"], keep[7])
        self.assertEqual(["child_of_deleted_index"], delete[5])
        self.assertEqual(["old_orphan_untagged"], delete[6])
        self.assertEqual(["old_unprotected_candidate"], delete[4])

    def test_invalid_inventory_is_rejected(self):
        for broken in ([technical(1, 1), technical(1, 2, n=9)], [{**technical(1, 1), "name": "latest"}],
                       [{**technical(1, 1), "created_at": "yesterday"}],
                       [{**technical(1, 1), "metadata": {"container": {"tags": "x"}}}]):
            with self.subTest(broken=broken), self.assertRaises(retention.RetentionError):
                retention.normalize(broken)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.iac = IacHistory(self)
        self.iac.commit("dev", {"pipeline": (IMAGE, digest(11))})
        self.env = {"PACKAGE_OWNER": "pipesor-org", "PACKAGE_NAME": "pipesor-pipeline-service",
                    "REPOSITORY": "pipesor-org/pipesor-pipeline-service", "IAC_PATH": str(self.iac.root),
                    "KEEP_RECENT": "3", "MIN_AGE_DAYS": "30"}
        self.versions = [technical(vid, 100 - vid) for vid in range(1, 13)] + [version(50, 99, [], n=50)]
        self.manifests = {digest(1): {"mediaType": INDEX, "manifests": [{"digest": digest(50)}]}}

    def test_dry_run_reports_without_deleting(self):
        github = FakeGitHub(self.versions, self.manifests)
        report = retention.run(self.env, github=github, now=NOW)
        self.assertEqual("dry-run", report["mode"])
        self.assertEqual([], github.deleted)
        self.assertEqual({"dev": [digest(11)]}, report["protection"]["deployed"])
        kept = {r["id"] for r in report["keep"]}
        self.assertEqual({10, 11, 12}, kept)
        self.assertIn("would delete 10", retention.summary(report))

    def test_delete_mode_removes_parents_before_children_and_respects_releases(self):
        release = {"tag_name": "v0.1.0", "assets": [{"id": 7, "name": "candidate.json"}]}
        github = FakeGitHub(self.versions, self.manifests, releases=[release],
                            assets={7: {"image": IMAGE, "digest": digest(3)}},
                            tags=[{"name": "v0.2.0", "commit": {"sha": source(4)}}, {"name": "nightly", "commit": {"sha": source(5)}}])
        report = retention.run({**self.env, "DELETE": "true"}, github=github, now=NOW)
        self.assertNotIn(3, github.deleted)
        self.assertNotIn(4, github.deleted)
        self.assertIn(5, github.deleted)
        self.assertEqual(github.deleted, report["deleted"])
        self.assertLess(github.deleted.index(1), github.deleted.index(50))

    def test_safety_limits_and_untrusted_inputs_stop_before_deleting(self):
        github = FakeGitHub(self.versions, self.manifests)
        with self.assertRaisesRegex(retention.RetentionError, "MAX_DELETIONS"):
            retention.run({**self.env, "DELETE": "true", "MAX_DELETIONS": "2"}, github=github, now=NOW)
        self.assertEqual([], github.deleted)
        with self.assertRaisesRegex(retention.RetentionError, "safety floor"):
            retention.run({**self.env, "MIN_AGE_DAYS": "1"}, github=github, now=NOW)
        with self.assertRaisesRegex(retention.RetentionError, "PROTECTED_DIGESTS"):
            retention.run({**self.env, "PROTECTED_DIGESTS": "latest"}, github=github, now=NOW)
        broken = FakeGitHub([version(1, 100, ["sha-" + source(1)], n=1)], {})
        broken.manifest = lambda *args: (_ for _ in ()).throw(retention.RetentionError("Manifest returned 500"))
        with self.assertRaisesRegex(retention.RetentionError, "Manifest"):
            retention.run({**self.env, "DELETE": "true"}, github=broken, now=NOW)
        self.assertEqual([], broken.deleted)

    def test_partial_deletion_failure_keeps_the_record_of_deleted_versions(self):
        github = FakeGitHub(self.versions, self.manifests, fail_delete=2)
        with self.assertRaises(retention.RetentionError) as raised:
            retention.run({**self.env, "DELETE": "true"}, github=github, now=NOW)
        deleted = raised.exception.report["deleted"]
        self.assertEqual(github.deleted, deleted)
        self.assertNotIn(2, deleted)
        self.assertNotIn(1, deleted)  # versions after the failure are not attempted
        self.assertIn("403", raised.exception.report["error"])

    def test_manual_protection_keeps_a_digest(self):
        github = FakeGitHub(self.versions, self.manifests)
        report = retention.run({**self.env, "PROTECTED_DIGESTS": f"{digest(2)}, {digest(6)}"}, github=github, now=NOW)
        kept = {r["id"]: r["reasons"] for r in report["keep"]}
        self.assertEqual(["protected_digest"], kept[2])
        self.assertEqual(["protected_digest"], kept[6])


if __name__ == "__main__":
    unittest.main()
