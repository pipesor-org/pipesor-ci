# AGENTS.md

Shared CI for every Pipesor repository: a change here affects all consumers once they update
their pinned SHA. Follow the organization [contribution policy](https://github.com/pipesor-org/.github/blob/main/CONTRIBUTING.md).

## Rules

- Keep workflows least-privilege: `permissions: {}` at the top, explicit job permissions, no cloud
  credentials, `persist-credentials: false`, inputs passed through `env` (never interpolated into
  `run`).
- Pin third-party actions and images by full SHA/digest with a version comment.
- Compose this repository's own workflows/actions with `uses: $/...`.
- `commitlint.config.js` and `lefthook.yml` are the canonical copies. When changing them, update the
  embedded copies in `conventions.yml` in the same change and plan the consumer rollout.
- Changing inputs/outputs or gate behavior incompatibly is a breaking change (`!` + `BREAKING
  CHANGE:` footer) and a new major tag.
- Do not add deployment to application repositories here; environment images are owned by
  pipesor-iac.

## Verification

Run before opening a PR: `bun install --frozen-lockfile`, `bun run test`, `actionlint`,
`zizmor --min-severity medium .`. Report hosted results from the PR checks.
