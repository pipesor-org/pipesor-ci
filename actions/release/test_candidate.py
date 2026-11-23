import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import candidate as module  # noqa: E402


class CandidateFetchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.repository = "pipesor-org/pipesor-pipeline-service"
        self.candidate = {"schema_version": 1, "source_sha": "a" * 40,
                          "image": "ghcr.io/" + self.repository, "digest": "sha256:" + "b" * 64,
                          "build_run_id": "42", "build_run_attempt": "2"}
        self.run = {"id": 42, "run_attempt": 2, "event": "push", "head_branch": "main",
                    "path": ".github/workflows/ci.yml", "status": "completed", "conclusion": "success",
                    "head_sha": self.candidate["source_sha"], "repository": {"full_name": self.repository}}
        self.env = {"GITHUB_REPOSITORY": self.repository, "CANDIDATE_RUN_ID": "42", "CANDIDATE_RUN_ATTEMPT": "2"}
        self.candidate_path = self.root / "candidate" / "candidate.json"
        self.run_path = self.root / "candidate-run.json"

    def write_candidate(self, data):
        self.candidate_path.parent.mkdir(parents=True, exist_ok=True)
        self.candidate_path.write_text(json.dumps(data))

    @patch.object(module, "gh")
    def test_ci_run_artifact_and_original_attempt_are_retrieved(self, github):
        self.write_candidate(self.candidate)
        github.side_effect = ["", json.dumps(self.run)]
        module.fetch_candidate(self.candidate_path, self.run_path, self.env)
        self.assertIn("candidate-42-2", github.call_args_list[0].args)
        self.assertEqual(("api", f"repos/{self.repository}/actions/runs/42/attempts/2"), github.call_args_list[-1].args)
        self.assertEqual(self.run, json.loads(self.run_path.read_text()))

    @patch.object(module, "gh")
    def test_release_download_preserves_original_ci_verification(self, github):
        release = "v1.2.3-rc.1"
        env = {**self.env, "CANDIDATE_RELEASE": release, "CANDIDATE_RUN_ID": "", "CANDIDATE_RUN_ATTEMPT": ""}
        self.write_candidate({**self.candidate, "release_version": release[1:]})
        github.side_effect = [json.dumps({"tag_name": release, "draft": False}), "",
                              json.dumps({"sha": self.candidate["source_sha"]}), json.dumps(self.run)]
        module.fetch_candidate(self.candidate_path, self.run_path, env)
        self.assertIn(self.repository, github.call_args_list[1].args)
        self.assertEqual(("api", f"repos/{self.repository}/actions/runs/42/attempts/2"), github.call_args_list[-1].args)

    @patch.object(module, "gh")
    def test_ambiguous_foreign_and_malformed_selections_are_rejected_before_download(self, github):
        for release in ("v01.0.0", "v1.0.0-rc.0", "other/repo/v1.0.0", "v1.0.0; exit 0", "latest"):
            with self.subTest(release=release), self.assertRaises(ValueError):
                module.fetch_candidate(self.candidate_path, self.run_path,
                                       {**self.env, "CANDIDATE_RELEASE": release, "CANDIDATE_RUN_ID": "",
                                        "CANDIDATE_RUN_ATTEMPT": ""})
        with self.assertRaisesRegex(ValueError, "not both"):
            module.fetch_candidate(self.candidate_path, self.run_path, {**self.env, "CANDIDATE_RELEASE": "v1.0.0"})
        for run_id, attempt in (("", ""), ("0", "1"), ("1", "x"), ("1; true", "1")):
            with self.subTest(run=run_id, attempt=attempt), self.assertRaises(ValueError):
                module.fetch_candidate(self.candidate_path, self.run_path,
                                       {**self.env, "CANDIDATE_RUN_ID": run_id, "CANDIDATE_RUN_ATTEMPT": attempt})
        with self.assertRaisesRegex(ValueError, "repository"):
            module.fetch_candidate(self.candidate_path, self.run_path, {**self.env, "GITHUB_REPOSITORY": "../x"})
        github.assert_not_called()

    @patch.object(module, "gh")
    def test_draft_release_and_foreign_tag_commit_are_rejected(self, github):
        env = {**self.env, "CANDIDATE_RELEASE": "v1.0.0", "CANDIDATE_RUN_ID": "", "CANDIDATE_RUN_ATTEMPT": ""}
        github.side_effect = [json.dumps({"tag_name": "v1.0.0", "draft": True})]
        with self.assertRaisesRegex(ValueError, "published"):
            module.fetch_candidate(self.candidate_path, self.run_path, env)
        self.write_candidate(self.candidate)
        github.side_effect = [json.dumps({"tag_name": "v1.0.0", "draft": False}), "", json.dumps({"sha": "c" * 40})]
        with self.assertRaisesRegex(ValueError, "source mismatch"):
            module.fetch_candidate(self.candidate_path, self.run_path, env)

    @patch.object(module, "gh")
    def test_malformed_manifest_run_identity_is_rejected(self, github):
        for value in (42, "0", "01", "", None):
            with self.subTest(value=value):
                self.write_candidate({**self.candidate, "build_run_id": value})
                github.side_effect = [""]
                with self.assertRaisesRegex(ValueError, "build_run_id"):
                    module.fetch_candidate(self.candidate_path, self.run_path, self.env)


if __name__ == "__main__":
    unittest.main()
