# Contributing to adk-tools

`adk-tools` is a **version lockfile plus a build recipe**: the tools live in
their own repositories and are pinned here as git submodules. One commit of this
repo pins one exact, verified combination of every tool.

## Branching model

- **`main`** -- stable. Every commit is release-quality and passed the verify
  gate. Release tags (`vYYYY.MM`) live here. Do not push tool bumps here
  directly.
- **`dev`** -- integration. Submodule bumps and new tools land here first and get
  verified, then promote to `main` at a release.
- **`feature/*`** -- short-lived branches cut from `dev`, merged back into `dev`.

## What enforces the model

A ruleset covers `main` and `dev`, so the section above is a condition on
merging rather than a convention. Both lines take changes only through a pull
request, neither can be deleted or force-pushed, and one status check is
required to merge: **`ci-gate`**. It collects the jobs in
`.github/workflows/ci.yml`, which check that every submodule pin is publicly
reachable on the branch it declares, that every path the image build reaches for
exists in the pinned tree, and that the tracked ruleset payloads are well
formed. It runs in under a minute and compiles nothing. Release tags (`v*`) are
protected against deletion and against being moved onto a different commit.

The payloads for every repository in the ecosystem live in `ci/rulesets/` and
are applied with `ci/rulesets/apply_rulesets.py`, never through the GitHub web
UI. `ci/rulesets/README.md` gives the reason, which is sharper than a style
preference. Repository admins hold a permanent bypass, so the ruleset is binding
for contributors and advisory for a maintainer who needs a force push on the day
a history rewrite is genuinely required.

## The verify gate

`build.sh` always builds the `verify` stage first (regenerates the demo
headless and runs every tool's test suite inside the image). A combination that
does not pass verify must never land on `main`. `--skip-verify` is for local
iteration only.

## Where to look

- **`docs/MAINTAINING.md`** -- how to bump a tool, add a new tool as a submodule,
  remove one, and cut a release. Read it before touching submodules or the
  `Dockerfile`.
- **`docs/RELEASE_CHECKLIST.md`** -- the ordered release checklist, and the
  record of the one-time work done for the first public release.
- **`CHANGELOG.md`** -- release history.
- **`NOTICE.md`** -- per-component licenses and the GPL corresponding-source
  obligation. Update it whenever you add or change a bundled component.

## Commit conventions

- English, concise, imperative. Match the existing history, e.g.
  `Bump <tool> pin to <ref> (<summary>)`.
- One logical change per commit; keep each tool bump in its own commit so
  regressions bisect cleanly.
- Do not hand-edit `manifest.json`; `build.sh` generates it (and it is
  gitignored).
