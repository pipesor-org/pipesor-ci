"""Retrieve a published candidate manifest and the CI attempt that produced it.

The manifest comes either from the original main CI run artifact or from a published
GitHub Release (RC/stable). The CI attempt metadata is always re-read from GitHub so the
release step can verify provenance; nothing is rebuilt or written remotely.
"""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

NUMBER = r"(?:0|[1-9][0-9]*)"
RELEASE = re.compile(r"v" + NUMBER + r"\." + NUMBER + r"\." + NUMBER + r"(?:-rc\.[1-9][0-9]*)?")
POSITIVE = re.compile(r"[1-9][0-9]*")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def selection(env):
    repository = env.get("GITHUB_REPOSITORY", "")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/(?!\.\.?$)[A-Za-z0-9_.-]+", repository), "Invalid repository")
    release = env.get("CANDIDATE_RELEASE", "")
    run_id, attempt = env.get("CANDIDATE_RUN_ID", ""), env.get("CANDIDATE_RUN_ATTEMPT", "")
    if release:
        require(RELEASE.fullmatch(release), "Invalid release version")
        require(not run_id and not attempt, "Choose a release or a CI run, not both")
    else:
        require(all(POSITIVE.fullmatch(value) for value in (run_id, attempt)), "Invalid CI run/attempt")
    return repository, release


def gh(*args):
    result = subprocess.run(["gh", *args], text=True, capture_output=True, check=False)
    require(result.returncode == 0, "GitHub candidate retrieval failed")
    return result.stdout


def fetch_candidate(candidate_path, run_path, env):
    repository, release = selection(env)
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    if release:
        metadata = json.loads(gh("api", f"repos/{repository}/releases/tags/{release}"))
        require(metadata.get("tag_name") == release and metadata.get("draft") is False,
                "Candidate release must be published")
        gh("release", "download", release, "--repo", repository, "--pattern", "candidate.json",
           "--dir", str(candidate_path.parent))
    else:
        gh("run", "download", env["CANDIDATE_RUN_ID"], "--repo", repository,
           "--name", f"candidate-{env['CANDIDATE_RUN_ID']}-{env['CANDIDATE_RUN_ATTEMPT']}",
           "--dir", str(candidate_path.parent))
    candidate = json.loads(candidate_path.read_text())
    for key in ("build_run_id", "build_run_attempt"):
        require(isinstance(candidate.get(key), str) and POSITIVE.fullmatch(candidate[key]), f"Invalid {key}")
    if release:
        commit = json.loads(gh("api", f"repos/{repository}/commits/{release}"))
        require(commit.get("sha") == candidate.get("source_sha"), "Release tag/candidate source mismatch")
    run_path.write_text(gh("api", f"repos/{repository}/actions/runs/{candidate['build_run_id']}"
                          f"/attempts/{candidate['build_run_attempt']}"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("fetch",))
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--run", required=True, type=Path)
    args = parser.parse_args()
    try:
        fetch_candidate(args.candidate, args.run, os.environ)
    except (ValueError, OSError, KeyError, TypeError, AttributeError, json.JSONDecodeError):
        # Do not print external payloads or exception details that could contain credentials.
        parser.exit(1, "Candidate retrieval rejected or failed; check the selected run/attempt or release.\n")


if __name__ == "__main__":
    main()
