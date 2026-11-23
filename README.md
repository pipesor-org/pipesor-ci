# pipesor-ci

Shared GitHub Actions workflows and delivery tooling for the Pipesor repositories. They implement
the organization's [contribution policy](https://github.com/pipesor-org/.github/blob/main/CONTRIBUTING.md)
(trunk-based development, Conventional Commits, squash merge, immutable artifact promotion) and the
[CI/CD guide](https://github.com/pipesor-org/pipesor-docs/blob/main/docs/development/cicd.md).
Repository READMEs keep their own build, test and runtime instructions; this repository owns the
common pipeline.

## Catalog

| Building block | Kind | Purpose |
|---|---|---|
| [`service-ci.yml`](.github/workflows/service-ci.yml) | Reusable workflow | Full CI for a container-delivered repository: conventions → verify/build/check once → image assurance → on main, publish the same archive. |
| [`conventions.yml`](.github/workflows/conventions.yml) | Reusable workflow | Canonical commit tooling, PR title (≤ 65 characters) and description, Conventional Commits for PR commits and the integrated main commit, actionlint and zizmor. Every repository runs it. |
| [`container-build.yml`](.github/workflows/container-build.yml) | Reusable workflow | Runs the repository verification, builds `ghcr.io/<repo>:sha-<sha>-<run>-<attempt>` once, checks the OCI revision label and a non-root user, runs the repository image checks and hands the exact archive to later jobs (1-day artifact). |
| [`image-assurance.yml`](.github/workflows/image-assurance.yml) | Reusable workflow | Trivy CycloneDX SBOM and vulnerability report of that archive (90-day evidence) and a gate on **fixable HIGH/CRITICAL** vulnerabilities. Reviewed exceptions live in the repository's `.trivyignore.yaml`. |
| [`publish-container.yml`](.github/workflows/publish-container.yml) | Reusable workflow | Main pushes only: loads the verified archive, keeps the GHCR package private, pushes without rebuilding and records `candidate-<run>-<attempt>/candidate.json` (90 days). |
| [`actions/release`](actions/release/action.yml) | Composite action | Names an existing candidate digest with `vX.Y.Z[-rc.N]` and a GitHub Release (`candidate.json` asset). Never rebuilds or retags the registry; stable requires retained preprod validation. |

Callers stay thin. A Java service, for example:

```yaml
name: CI

on:
  pull_request:
    branches: [main]
    types: [opened, synchronize, reopened, edited]
  push:
    branches: [main]

permissions: {}

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}

jobs:
  ci:
    uses: pipesor-org/pipesor-ci/.github/workflows/service-ci.yml@<full commit SHA> # v1.0.0
    permissions:
      contents: read
      packages: write
    with:
      java-version: '25'
      verify-command: python3 -m unittest discover -s tools -p 'test_*.py' && ./mvnw -B -ntp clean verify
      test-reports: target/surefire-reports/TEST-*.xml
```

Repositories without an image (IaC, docs, landing, workspace) call `conventions.yml` and keep their
own verification job in the same `ci.yml`. The build workflow must be `.github/workflows/ci.yml`:
release tooling verifies that a candidate came from a successful `ci.yml` push run on main.

## Repository contract

Every consuming repository has:

- `package.json` (Bun 1.4.4) with `@commitlint/cli` and `@commitlint/config-conventional` 21.2.4,
  the `commitlint` and `setup:hooks` scripts and a committed `bun.lock`.
- `commitlint.config.js` and `lefthook.yml` identical to the copies in this repository
  ([commitlint](commitlint.config.js), [Lefthook](lefthook.yml)); `conventions.yml` fails on drift.
- A `.github/dependabot.yml` for `github-actions` (weekly, grouped, 7-day cooldown) so pinned SHAs
  stay current.
- For images: a Dockerfile that accepts `VCS_REF` and labels `org.opencontainers.image.revision`,
  runs as a non-root user, and a repository image check (default `python3 tools/check_image.py
  "$IMAGE"`) for service-specific smoke tests.

## Artifact identity, promotion and deployment

- The technical tag `sha-<sha>-<run>-<attempt>` is unique per run; the identity is the digest.
  `candidate.json` (schema 1: `source_sha`, `image`, `digest`, `build_run_id`, `build_run_attempt`)
  is the handoff to releases and environments.
- PR builds are verified and scanned but never published. Main re-verifies the integrated commit.
- **Deployment is not done from application repositories.** The environment's Terraform root in
  [pipesor-iac](https://github.com/pipesor-org/pipesor-iac) owns the image of every Container App and
  pins it by digest; promotion means the same digest in the next environment's reviewed plan
  (dev → qa → preprod → prod). Rollback is a new reviewed plan with the earlier digest.
- Releases are optional names for a digest. The release workflow in each service is disabled in
  source until activation is authorized; stable requires the preprod validation contract.

## Versioning and consumption

- Tags `vMAJOR.MINOR.PATCH`. Breaking input/behavior changes bump MAJOR.
- Consumers pin the **full commit SHA** of a tag with a `# vX.Y.Z` comment; Dependabot proposes
  updates. Workflows inside this repository compose each other with the self-repository syntax
  (`uses: $/...`), so a pinned caller always runs one consistent commit.
- The repository is public because the consuming repositories are public; workflows never receive
  cloud credentials. Published images stay private.

## Development

```bash
bun install --frozen-lockfile
bun run setup:hooks
bun run test                      # release planning tests (Bun) and candidate retrieval tests (Python)
actionlint                        # .github/actionlint.yaml documents the `$/` exception
zizmor --min-severity medium .    # with GH_TOKEN for online audits
```

Tool versions used by the workflows: Lefthook 2.2.2, actionlint 1.7.12, zizmor 1.33.1 (SHA-256
verified downloads), Trivy 0.76.2 (digest-pinned image).
