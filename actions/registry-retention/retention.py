"""Plan, and optionally apply, GHCR retention for one repository's container package.

A version is kept when any rule protects it:
- its digest is (or recently was) deployed: the current and the last KEEP_DEPLOYED previous distinct
  entries of every environments/*/images.json in the IaC repository history;
- it is named by a release: the candidate.json of any GitHub Release, or its source commit carries a v* tag;
- its tags are not exclusively technical candidate tags (sha-<sha>[-<run>-<attempt>]);
- it is one of the KEEP_RECENT newest technical candidates or younger than MIN_AGE_DAYS;
- it is a child (platform or attestation manifest) of a kept image index;
- it is listed in PROTECTED_DIGESTS.
Children of a deleted index are deleted with it. Any input that cannot be read stops the run
before deletion. Deletion happens only with DELETE=true and stays below MAX_DELETIONS.
"""

import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
TECHNICAL_TAG = re.compile(r"sha-([0-9a-f]{40})(?:-[1-9][0-9]*-[1-9][0-9]*)?")
RELEASE_TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-rc\.[1-9][0-9]*)?")
INDEX_TYPES = {"application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"}
MANIFEST_ACCEPT = ", ".join(sorted(INDEX_TYPES | {"application/vnd.oci.image.manifest.v1+json",
                                                  "application/vnd.docker.distribution.manifest.v2+json"}))


class RetentionError(Exception):
    """Input that cannot be trusted or a failed deletion; the report records what was deleted."""

    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report


def require(condition, message):
    if not condition:
        raise RetentionError(message)


def parse_time(value):
    require(isinstance(value, str), "Missing timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RetentionError(f"Invalid timestamp {value!r}") from None
    require(parsed.tzinfo is not None, "Timestamps must carry a time zone")
    return parsed


# --- Inputs -----------------------------------------------------------------------------------

def deployed_digests(iac_root, image, keep_deployed):
    """Current and recent distinct digests of `image` per environment, from images.json history."""
    root = Path(iac_root)
    require((root / ".git").exists(), f"{iac_root} is not a Git checkout with history")
    pathspec = "environments/*/images.json"
    commits = git(root, "log", "--format=%H", "--", pathspec).split()
    require(commits, "No environments/*/images.json history found in the IaC repository")
    history = {}
    for commit in commits:  # newest first
        for path in git(root, "ls-tree", "-r", "--name-only", commit, "--", "environments").split():
            match = re.fullmatch(r"environments/([a-z0-9-]+)/images\.json", path)
            if not match:
                continue
            try:
                document = json.loads(git(root, "show", f"{commit}:{path}"))
            except json.JSONDecodeError:
                raise RetentionError(f"{path} at {commit[:12]} is not valid JSON") from None
            require(document.get("schema_version") == 1, f"{path} at {commit[:12]} uses an unknown schema")
            digests = history.setdefault(match.group(1), [])
            for entry in document.get("images", {}).values():
                if entry.get("image") == image:
                    digest = entry.get("digest")
                    require(isinstance(digest, str) and DIGEST.fullmatch(digest), f"Invalid digest in {path}")
                    if digest not in digests:
                        digests.append(digest)
    return {environment: digests[: 1 + keep_deployed] for environment, digests in history.items()}


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    require(result.returncode == 0, f"git {args[0]} failed in the IaC checkout")
    return result.stdout


class GitHub:
    def __init__(self, token, api="https://api.github.com", registry="https://ghcr.io"):
        require(token, "GH_TOKEN is required")
        self.token, self.api, self.registry = token, api.rstrip("/"), registry.rstrip("/")
        self._registry_token = None

    def _open(self, url, headers, method="GET", authorization=None):
        request = urllib.request.Request(url, method=method, headers=headers)
        if authorization:
            # Never forward credentials to redirect targets (release assets live on object storage).
            request.add_unredirected_header("Authorization", authorization)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def api_get(self, path, accept="application/vnd.github+json"):
        status, _, body = self._open(f"{self.api}{path}", {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"},
                                     authorization=f"Bearer {self.token}")
        require(status == 200, f"GitHub API {path.split('?')[0]} returned {status}")
        return body

    def paginate(self, path):
        items, url = [], f"{self.api}{path}"
        while url:
            status, headers, body = self._open(url, {"Accept": "application/vnd.github+json",
                                                     "X-GitHub-Api-Version": "2022-11-28"},
                                               authorization=f"Bearer {self.token}")
            require(status == 200, f"GitHub API {path.split('?')[0]} returned {status}")
            items.extend(json.loads(body))
            links = {part.split(";")[1].strip(): part.split(";")[0].strip()[1:-1]
                     for part in headers.get("Link", headers.get("link", "")).split(",") if ";" in part}
            url = links.get('rel="next"')
        return items

    def delete_version(self, owner, package, version_id, missing_ok=False):
        status, _, _ = self._open(f"{self.api}/orgs/{owner}/packages/container/{package}/versions/{version_id}",
                                  {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
                                  method="DELETE", authorization=f"Bearer {self.token}")
        # A child manifest may disappear together with its deleted index.
        require(status == 204 or (missing_ok and status == 404),
                f"Deleting version {version_id} returned {status}; check the package's Actions access")

    def manifest(self, owner, package, digest):
        if self._registry_token is None:
            scope = urllib.parse.quote(f"repository:{owner}/{package}:pull")
            basic = base64.b64encode(f"token:{self.token}".encode()).decode()
            status, _, body = self._open(f"{self.registry}/token?scope={scope}&service=ghcr.io", {},
                                         authorization=f"Basic {basic}")
            require(status == 200, f"Registry token request returned {status}")
            self._registry_token = json.loads(body)["token"]
        status, _, body = self._open(f"{self.registry}/v2/{owner}/{package}/manifests/{digest}",
                                     {"Accept": MANIFEST_ACCEPT}, authorization=f"Bearer {self._registry_token}")
        require(status == 200, f"Manifest {digest[:19]} returned {status}")
        return json.loads(body)


def release_protection(github, repository, image):
    """Digests named by release manifests and commits carrying v* tags."""
    digests, sources = set(), set()
    for release in github.paginate(f"/repos/{repository}/releases?per_page=100"):
        for asset in release.get("assets", []):
            if asset.get("name") != "candidate.json":
                continue
            manifest = json.loads(github.api_get(f"/repos/{repository}/releases/assets/{asset['id']}",
                                                 accept="application/octet-stream"))
            if manifest.get("image") == image:
                require(DIGEST.fullmatch(manifest.get("digest", "")), f"Invalid digest in release {release.get('tag_name')}")
                digests.add(manifest["digest"])
    for tag in github.paginate(f"/repos/{repository}/tags?per_page=100"):
        if RELEASE_TAG.fullmatch(tag.get("name", "")):
            sources.add(tag["commit"]["sha"])
    return digests, sources


def index_children(github, owner, package, versions):
    """Map child digest -> parent version IDs for every tagged image index."""
    children = {}
    for version in versions:
        if not version["tags"]:
            continue
        manifest = github.manifest(owner, package, version["digest"])
        if manifest.get("mediaType") in INDEX_TYPES:
            for child in manifest.get("manifests", []):
                children.setdefault(child["digest"], set()).add(version["id"])
    return children


# --- Policy -----------------------------------------------------------------------------------

def normalize(inventory):
    versions, ids, digests = [], set(), set()
    for item in inventory:
        version_id, digest = item.get("id"), item.get("name")
        require(type(version_id) is int and version_id not in ids, "Invalid or duplicate version ID")
        require(isinstance(digest, str) and DIGEST.fullmatch(digest) and digest not in digests,
                "Invalid or duplicate version digest")
        tags = item.get("metadata", {}).get("container", {}).get("tags")
        require(isinstance(tags, list) and all(isinstance(tag, str) for tag in tags), "Invalid container tags")
        sources = {match.group(1) for tag in tags if (match := TECHNICAL_TAG.fullmatch(tag))}
        versions.append({"id": version_id, "digest": digest, "created_at": parse_time(item.get("created_at")),
                         "tags": tags, "technical": bool(tags) and all(TECHNICAL_TAG.fullmatch(tag) for tag in tags),
                         "sources": sources})
        ids.add(version_id)
        digests.add(digest)
    return versions


def plan(versions, *, children, protected_digests, protected_sources, now, min_age_days, keep_recent):
    recent_cutoff = now - timedelta(days=min_age_days)
    technical = sorted((v for v in versions if v["technical"]), key=lambda v: (v["created_at"], v["id"]), reverse=True)
    newest = {v["id"] for v in technical[:keep_recent]}
    decisions = {}
    for version in versions:
        if not version["tags"]:
            continue
        reasons = []
        if version["digest"] in protected_digests:
            reasons.append("protected_digest")
        if version["sources"] & protected_sources:
            reasons.append("release_source")
        if not version["technical"]:
            reasons.append("non_technical_tags")
        if version["id"] in newest:
            reasons.append("newest_candidates")
        if version["created_at"] > recent_cutoff:
            reasons.append("recent")
        decisions[version["id"]] = reasons or ["old_unprotected_candidate"]
    kept_parents = {vid for vid, reasons in decisions.items() if reasons != ["old_unprotected_candidate"]}
    for version in versions:
        if version["tags"]:
            continue
        parents = children.get(version["digest"], set())
        if version["digest"] in protected_digests:
            decisions[version["id"]] = ["protected_digest"]
        elif parents & kept_parents:
            decisions[version["id"]] = ["child_of_kept_index"]
        elif parents:
            decisions[version["id"]] = ["child_of_deleted_index"]
        elif version["created_at"] > recent_cutoff:
            decisions[version["id"]] = ["recent_untagged"]
        else:
            decisions[version["id"]] = ["old_orphan_untagged"]
    deletable = {"old_unprotected_candidate", "child_of_deleted_index", "old_orphan_untagged"}
    keep, delete = [], []
    for version in sorted(versions, key=lambda v: (v["created_at"], v["id"]), reverse=True):
        record = {"id": version["id"], "digest": version["digest"], "tags": version["tags"],
                  "created_at": version["created_at"].isoformat(), "reasons": decisions[version["id"]]}
        (delete if decisions[version["id"]][0] in deletable else keep).append(record)
    # Parents first so an index never points to an already deleted child.
    delete.sort(key=lambda record: 0 if record["tags"] else 1)
    return keep, delete


# --- Entry point --------------------------------------------------------------------------------

def run(env, github=None, now=None):
    owner, package = env["PACKAGE_OWNER"], env["PACKAGE_NAME"]
    repository = env["REPOSITORY"]
    require(re.fullmatch(r"[A-Za-z0-9-]+", owner) and re.fullmatch(r"[A-Za-z0-9_.-]+", package), "Invalid package")
    image = f"ghcr.io/{owner.lower()}/{package.lower()}"
    policy = {key: int(env.get(name, default)) for key, name, default in (
        ("min_age_days", "MIN_AGE_DAYS", 30), ("keep_recent", "KEEP_RECENT", 10),
        ("keep_deployed", "KEEP_DEPLOYED", 3), ("max_deletions", "MAX_DELETIONS", 100))}
    require(policy["min_age_days"] >= 7 and policy["keep_recent"] >= 3 and policy["keep_deployed"] >= 1,
            "Policy below the safety floor (7 days, 3 recent candidates, 1 previous deployment)")
    delete_requested = env.get("DELETE", "false") == "true"
    github = github or GitHub(env.get("GH_TOKEN", ""))
    now = now or datetime.now(timezone.utc)

    manual = {value for value in re.split(r"[\s,]+", env.get("PROTECTED_DIGESTS", "")) if value}
    require(all(DIGEST.fullmatch(value) for value in manual), "PROTECTED_DIGESTS must list sha256 digests")
    deployed = deployed_digests(env["IAC_PATH"], image, policy["keep_deployed"])
    release_digests, release_sources = release_protection(github, repository, image)
    versions = normalize(github.paginate(f"/orgs/{owner}/packages/container/{package}/versions?per_page=100"))
    children = index_children(github, owner, package, versions)
    protected = manual | release_digests | {digest for digests in deployed.values() for digest in digests}
    keep, delete = plan(versions, children=children, protected_digests=protected, protected_sources=release_sources,
                        now=now, min_age_days=policy["min_age_days"], keep_recent=policy["keep_recent"])
    report = {"schema_version": 1, "image": image, "generated_at": now.isoformat(), "policy": policy,
              "mode": "delete" if delete_requested else "dry-run",
              "protection": {"deployed": deployed, "releases": sorted(release_digests),
                             "release_sources": sorted(release_sources), "manual": sorted(manual)},
              "summary": {"versions": len(versions), "tagged": sum(1 for v in versions if v["tags"]),
                          "keep": len(keep), "delete": len(delete)},
              "keep": keep, "delete": delete, "deleted": []}
    if delete_requested and delete:
        require(len(delete) <= policy["max_deletions"],
                f"{len(delete)} deletions exceed MAX_DELETIONS={policy['max_deletions']}; review the dry-run report")
        try:
            for record in delete:
                github.delete_version(owner, package, record["id"], missing_ok=not record["tags"])
                report["deleted"].append(record["id"])
        except RetentionError as error:
            report["error"] = str(error)
            raise RetentionError(str(error), report) from None
    return report


def summary(report):
    deployed = "; ".join(f"{env}: {', '.join(d[7:19] for d in digests)}" for env, digests in report["protection"]["deployed"].items())
    lines = [f"## Registry retention: `{report['image']}` ({report['mode']})", "",
             f"{report['summary']['versions']} versions ({report['summary']['tagged']} tagged): "
             f"keep {report['summary']['keep']}, {'deleted' if report['mode'] == 'delete' else 'would delete'} "
             f"{len(report['deleted']) if report['mode'] == 'delete' else report['summary']['delete']}.", "",
             f"Protected deployments (current + recent): {deployed or 'none'}.",
             f"Release digests: {len(report['protection']['releases'])}; release source commits: "
             f"{len(report['protection']['release_sources'])}.", ""]
    if report["delete"]:
        lines += ["| Version | Digest | Tags | Created | Reason |", "|---|---|---|---|---|"]
        lines += [f"| {r['id']} | `{r['digest'][7:19]}` | {', '.join(r['tags']) or '—'} | {r['created_at'][:10]} | "
                  f"{', '.join(r['reasons'])} |" for r in report["delete"]]
    return "\n".join(lines) + "\n"


def main():
    output = Path(os.environ.get("REPORT_PATH", "registry-retention.json"))
    try:
        report = run(os.environ)
    except RetentionError as error:
        if error.report is not None:
            output.write_text(json.dumps(error.report, indent=2) + "\n")
            print(f"::error::Registry retention stopped after deleting {len(error.report['deleted'])} versions: {error}")
        else:
            print(f"::error::Registry retention stopped before any deletion: {error}")
        sys.exit(1)
    except (KeyError, ValueError, OSError) as error:
        print(f"::error::Registry retention stopped before any deletion: {error}")
        sys.exit(1)
    output.write_text(json.dumps(report, indent=2) + "\n")
    text = summary(report)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write(text)


if __name__ == "__main__":
    main()
