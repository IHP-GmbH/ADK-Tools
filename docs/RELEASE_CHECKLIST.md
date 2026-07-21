# Release checklist

Two parts:

- **A. Every release** — the routine for cutting any `vYYYY.MM` tag.
- **B. Before the first PUBLIC release** — one-time work that must land before
  the repo/image/submodules are exposed to anonymous users. **These are not yet
  done.** Today the project is internal (private repos, private registry).

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
- [ ] Build the release commit: `./build.sh` (produces `adk-tools:dev` + a clean
      `manifest.json`).
- [ ] Annotated tag on `main`: `git tag -a vYYYY.MM -m "..."` (add `-s` to sign
      if a GPG key is configured).
- [ ] Push: `git push origin main && git push origin vYYYY.MM`.
- [ ] Publish the image from the tagged build: `./release.sh vYYYY.MM`
      (**read part B first** if this is going to a public registry).
- [ ] Update `CHANGELOG.md`.
- [ ] Merge any post-release fixes back into `dev`.

---

## B. Before the first PUBLIC release (deferred, NOT done)

The project is currently **internal**: the seven IHP-GmbH source repos are
private, the ghcr package is private, and the docs say so on purpose. Going
public requires the following, roughly in order. Do not push a public tag until
every **[blocker]** is cleared.

### Source availability & licensing

- [ ] **[blocker]** Make the four GPL source repos public with each pinned
      commit reachable from a public branch/tag:
      `KiCad-ADK-MOD`, `chiplet-studio`, `Chiplets-KiCad-Plugin`, `gds2kicad`.
      The image ships them as compiled binaries; GPL §6 requires the
      corresponding source be available to any image recipient. While private,
      publishing the image is a GPL violation.
- [ ] **[blocker]** Make the remaining source repos reachable so their
      `NOTICE.md` attribution URLs resolve for the public: `ADK`,
      `OpenIntM4TM2`, `IHP-Interconnect-IntM4TM2`.
- [ ] Re-verify licensing from a **credential-free** shell (no `gh` token, no
      `.netrc`): `git ls-remote https://github.com/IHP-GmbH/<repo>.git` must
      return `200`, not `401`. A `gh` credential helper silently injects a token
      and makes private repos look public — do not trust an authenticated check.

### Clone & build path for keyless users

- [ ] **[blocker]** Switch `.gitmodules` URLs from `git@github.com:` (SSH) to
      `https://github.com/IHP-GmbH/<repo>.git`, then `git submodule sync`.
      SSH URLs break anonymous `git clone --recurse-submodules`.
- [ ] Update the README clone command(s) to the HTTPS URL.
- [ ] Maintainers who push over SSH keep it locally without editing
      `.gitmodules`:
      `git config --global url."git@github.com:".insteadOf https://github.com/`.
- [ ] **Definitive gate:** from a machine with **no** SSH keys and **no** `gh`
      auth, run `git clone --recurse-submodules
      https://github.com/IHP-GmbH/ADK-Tools.git` and confirm every submodule
      checks out at its pinned commit.

### Registry & distribution

- [ ] Make the `ghcr.io/ihp-gmbh/adk-tools` package public.
- [ ] Rewrite the distribution docs: remove the "internal use" / "keep private"
      / "embeds private-repo code" language (README quickstart and the ~line-152
      note; the `Dockerfile` `LABEL` comment "Keep both private."). Make the
      public image with immutable version tags a first-class channel.
- [x] **Redesign `release.sh`** — DONE. The script now requires an immutable
      `vYYYY.MM` tag (validated, and refused if already published), never prunes
      by default (opt-in `PRUNE_UNTAGGED=1` only, for the private quota), and
      gates the push on a clean tree + dirty/unpinned-submodule check + HEAD
      exactly at the tag + the baked manifest `meta` matching the tag (rejects a
      dirty/untagged image). This item is release-workflow code only and is
      independent of the go-public admin actions below.

### Reproducibility hardening (recommended for public)

- [ ] Pin the base image by digest in all three `FROM ubuntu:24.04` lines
      (`FROM ubuntu:24.04@sha256:...`).
- [ ] Consider pinning `kicad-symbols` / `kicad-footprints` by commit SHA
      instead of the mutable `9.0.9.1` tag.
- [ ] Document the required build-time egress (github.com + gitlab.com) and a
      minimum host spec (cores + RAM) in the README.

### History hygiene (maintainer decision required)

- [ ] The maintainer home path that was in tracked example files
      (`examples/two_die_interposer/kicad/...`) is fixed in the working tree
      from `v2026.07` on, but **still present in older git history** (introduced
      `ebe26e5`, removed `831ed0b`; the affected commits also carry a maintainer
      author email). **DECISION: scrub before the first public push.** Rewrite
      with `git filter-repo` to strip the `${HOME}/...` paths, then
      force-push the rewritten history to the IHP-GmbH remote. This rewrites
      shared history and MUST be coordinated with everyone holding a clone (they
      re-clone or reset), so it runs as a deliberate step immediately before the
      first public push, NOT during routine internal work.

### Image content sanity (run before any public push)

- [ ] Build fresh from a clean checkout so no local scratch is baked in
      (`.dockerignore` is an exclude list over the working tree; untracked
      scratch under `examples/` or `tools/` can otherwise enter the context).
- [ ] Confirm the baked `manifest.json` inside the image lists only the intended
      submodules and contains no filesystem path
      (`docker run --rm adk-tools:<tag> cat /opt/adk-tools/manifest.json`).
