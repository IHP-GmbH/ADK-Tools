# Release checklist

Two parts:

- **A. Every release** -- the routine for cutting any `vYYYY.MM` tag.
- **B. The first public release** -- one-time work that had to land before the
  repo and its submodules were exposed to anonymous users. **Done, at
  `v2026.07` (2026-07-22).** Kept as the record of what was decided and as the
  source of the standing rules that outlived it.

The project is public: this repository and the seven pinned source repositories
are anonymously clonable. The distribution channel is **source only**; no image
is published (see part B, Registry and distribution).

See `MAINTAINING.md` for the branching model and the mechanics behind each step.

---

## A. Every release (internal or public)

- [ ] `dev` is green: `./build.sh` passes on the tip of `dev`.
- [ ] Working tree and submodules are clean (`git status`, `git submodule
      status` show no local edits, no detached scratch commits).
- [ ] Every submodule pin is on its tracked branch and pushed to its remote
      (a pin that only exists on your machine breaks a fresh clone and the
      Docker submodule fetch).
- [ ] No local paths or secrets in tracked files:
      `git grep -nI "/home/" -- . ':(exclude)tools/*' ':(exclude)docs/*'` is empty
      (excluding `docs/` keeps this checklist's own example pattern from
      self-matching).
- [ ] Promote `dev -> main`.
- [ ] Put `main` back on the main track: reset every `.gitmodules` `branch`
      field to `main` and move each gitlink to that tool's `main` tip. The fold
      does not do this; `dev` pins the tools' `dev` branches.
- [ ] Build the release commit: `./build.sh` (produces `adk-tools:dev` + a clean
      `manifest.json`).
- [ ] Annotated tag on `main`: `git tag -a vYYYY.MM -m "..."` (add `-s` to sign
      if a GPG key is configured).
- [ ] Push: `git push origin main && git push origin vYYYY.MM`.
- [ ] Confirm the baked manifest is clean: every tool URL is an
      `https://github.com/` one and no filesystem path appears
      (`docker run --rm adk-tools:dev cat /opt/adk-tools/manifest.json`).
- [ ] Update `CHANGELOG.md`.
- [ ] Merge any post-release fixes back into `dev`.

---

## B. The first public release (done at `v2026.07`, 2026-07-22)

This is a record, not a to-do list. Everything marked `[x]` was executed for
`v2026.07`. The items that became **standing rules** say so; those apply to
every release and to every new tool, not just the first one.

### Source availability and licensing

- [x] **[blocker]** The four GPL source repos are public with each pinned commit
      reachable from a public branch: `KiCad-ADK-MOD`, `chiplet-studio`,
      `Chiplets-KiCad-Plugin`, `gds2kicad`. The image ships them as compiled
      binaries; GPL §6 requires the corresponding source be available to any
      image recipient.
- [x] **[blocker]** The remaining source repos are reachable, so the `NOTICE.md`
      attribution URLs resolve for the public: `IHP-Open-ADK` (renamed from
      `ADK`), `OpenIntM4TM2`, `IHP-Interconnect-IntM4TM2`.
- [x] **Standing rule.** Re-verify from a **credential-free** shell (no `gh`
      token, no `.netrc`, credential helper disabled) that every source repo
      resolves anonymously:
      ```bash
      GIT_TERMINAL_PROMPT=0 git -c credential.helper= \
          ls-remote https://github.com/IHP-GmbH/<repo>.git HEAD
      ```
      A `gh` credential helper silently injects a token and makes a private repo
      look public; never trust an authenticated check. Re-run this whenever a
      tool is added, and confirm the **pinned commit itself** is reachable from
      a public branch, not just the repository.

### Clone and build path for keyless users

- [x] **[blocker]** `.gitmodules` URLs are `https://github.com/IHP-GmbH/<repo>.git`,
      not `git@github.com:` (SSH breaks anonymous
      `git clone --recurse-submodules`). **Standing rule**, see
      `MAINTAINING.md` section 4.1.
- [x] The README clone command uses the HTTPS URL.
- [x] Maintainers who push over SSH rewrite it locally instead of editing the
      tracked file:
      `git config --global url."git@github.com:".insteadOf https://github.com/`.

### Registry and distribution

- [x] **Decision: no image is published.** The distribution channel is the
      source; users clone and run `./build.sh`. The
      `ghcr.io/ihp-gmbh/adk-tools` package stays private and is not part of the
      release flow. The docs were aligned to this. Revisit only as a deliberate
      decision; until then, no "pull the image" instruction anywhere.
- [x] The "internal use" / "keep private" / "embeds private-repo code" language
      is gone from the README and the `Dockerfile` `LABEL` comment.
- [x] **`release.sh` redesign.** The script requires an immutable `vYYYY.MM`
      tag (validated, refused if already published), never prunes by default
      (opt-in `PRUNE_UNTAGGED=1` only), and gates the push on a clean tree +
      dirty/unpinned-submodule check + `HEAD` exactly at the tag + the baked
      manifest `meta` matching the tag. It is kept working for the registry path
      even though that path is unused.

### History hygiene

- [x] The maintainer home path that had been in tracked example files was
      scrubbed from the git history with `git filter-repo` immediately before
      the first public push, and the rewritten history was force-pushed. The
      rewrite was content-only: no commit and no path was dropped.
      **Consequence, and it bites:** every branch cut before the rewrite is on a
      disjoint commit graph and must never be bare-rebased onto `main` or `dev`.
      See `MAINTAINING.md` section 2.
- [x] **Standing rule.** Before any push, tracked files carry no local path:
      ```bash
      git grep -nI "/home/" -- . ':(exclude)tools/*' ':(exclude)docs/*'
      ```
      must be empty (`docs/` is excluded so this checklist's own example pattern
      does not self-match).

### Reproducibility hardening (still open, recommended)

- [ ] Pin the base image by digest in all three `FROM ubuntu:24.04` lines
      (`FROM ubuntu:24.04@sha256:...`).
- [ ] Consider pinning `kicad-symbols` / `kicad-footprints` by commit SHA
      instead of the mutable `9.0.9.1` tag.
- [ ] Document the required build-time egress (github.com + gitlab.com) and a
      minimum host spec (cores + RAM) in the README.
- [ ] **Known gap: the build context is not the git tree.** `.dockerignore` is
      an exclude list over the working tree, so files that are untracked or
      git-ignored under `tools/` still enter the context and get baked into the
      image. `release.sh`'s provenance gate reads `git status` and
      `git submodule status`, so it cannot see them: an image built in a working
      checkout can differ from one built from a clean clone. Until this is
      closed, build a release image from a fresh clone.

### Image content sanity (run before any push)

- [ ] Build fresh from a clean checkout so no local scratch is baked in (see the
      known gap above).
- [ ] Confirm the baked `manifest.json` inside the image lists only the intended
      submodules and contains no filesystem path
      (`docker run --rm adk-tools:<tag> cat /opt/adk-tools/manifest.json`).
