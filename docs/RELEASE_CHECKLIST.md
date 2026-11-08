# Release checklist

The routine for cutting a `vYYYY.MM` tag. A release is this repository at the
tag; no image is published, to a registry or as a download, and people who run
a release build the image locally (`README.md`, Build). This repository and
every repository it pins as a submodule must be anonymously clonable at the tag.
The repositories the build stages from local checkouts (`ci/pin-set.json`,
`"reachable": "local-only"`) are not covered by that, a clone of this
repository alone does not build, and the release notes must not call those
repositories public.

See `MAINTAINING.md` for the branching model and the mechanics behind each step.

---

## Before the tag

- [ ] `dev` is green: `./build.sh` passes on the tip of `dev`.
- [ ] Working tree and submodules are clean (`git status`, `git submodule
      status` show no local edits, no detached scratch commits).
- [ ] Every submodule pin is on its tracked branch and pushed to its remote
      (a pin that only exists on your machine breaks a fresh clone and the
      Docker submodule fetch).
- [ ] Every pinned repository, and the pinned commit itself, resolves from a
      **credential-free** shell (no `gh` token, no `.netrc`, credential helper
      disabled):
      ```bash
      GIT_TERMINAL_PROMPT=0 git -c credential.helper= \
          ls-remote https://github.com/IHP-GmbH/<repo>.git HEAD
      ```
      A `gh` credential helper silently injects a token and makes a private
      repository look public; never trust an authenticated check.
- [ ] Every `.gitmodules` URL is `https://github.com/IHP-GmbH/<repo>.git`, not
      `git@github.com:` (SSH breaks anonymous `git clone --recurse-submodules`).
      See `MAINTAINING.md` section 4.1.
- [ ] The last `integration-floating` run is under 14 days old
      (`gh run list --workflow integration-floating.yml --limit 1`) and its
      drift issue, if one is open, has been read. An older run does not mean
      the ecosystem is quiet: GitHub disables `schedule` after 60 days without
      repository activity, and this date is the only place that shows. A
      `schedule` trigger fires only from the workflow on the default branch,
      so while `main` carries no `.github/workflows/integration-floating.yml`
      there is no run to read and this item does not apply.
- [ ] No local paths or secrets in tracked files: `git grep -nI "/home""/"`
      is empty. The pattern is split so this line does not match itself;
      submodules are checked in their own repositories.

## The tag

- [ ] Promote `dev -> main`.
- [ ] Put `main` back on the main track: reset every `.gitmodules` `branch`
      field to `main` and move each gitlink to that tool's `main` tip. The
      promotion does not do this; `dev` pins the tools' `dev` branches. The
      same applies to `ci/integration-refs.json`, which names the two
      repositories that are not submodules. The `gitmodules-track` contract
      checks both halves, so forgetting either turns the `integration` check
      red on `main`. Then regenerate `ci/pin-set.json` (`python3 ci/pin_set.py`).
- [ ] Annotated tag on `main`, local only for now: `git tag -a vYYYY.MM -m
      "..."` (add `-s` to sign if a GPG key is configured). It comes before
      the build because `build.sh` takes the image's `meta` from
      `git describe --tags`.
- [ ] Build the tagged commit from a fresh clone of this checkout, which
      carries the tag (`git clone --recurse-submodules <this checkout>`; see
      Known limits): `./build.sh` (produces `adk-tools:dev` and its
      `manifest.json`). A clone of `origin` has neither the promoted `main`
      nor the tag yet. The manifest takes tool URLs from `.gitmodules`, so a
      local clone puts no filesystem path in it. A clone outside the
      checkout's parent directory has no siblings beside it: set
      `STAGE_SIBLINGS` to the directory that holds them, and
      `STAGE_DIE_VIEWS_DIR` to the `die-views/` folder of an adk-examples
      checkout at the release, and run
      `./stage-sources.sh --check && ./stage-die-views.sh --check` first.
- [ ] Confirm the baked manifest is clean: `meta` reads `vYYYY.MM`, it lists
      only the intended submodules, every tool URL is an `https://github.com/`
      one and no filesystem path appears
      (`docker run --rm adk-tools:dev cat /opt/adk-tools/manifest.json`).
      If not, delete the local tag, fix on `dev`, and start again.
- [ ] Each staged tree's license marks (SPDX lines, REUSE files, `.license`
      sidecars) match its row in `NOTICE.md` ("Staged components"). The
      check reads what this image ships: `staging/<tree>/` as `build.sh`
      (through `./stage-sources.sh`) left it in the fresh clone, each tree a
      clone at the pinned commit. Run the same block once after the pin
      reset, with `./stage-sources.sh` alone, to find a mismatch before the
      tag. `staged-licenses.txt` is a record kept with the release's
      evidence; it is not in the image. From the fresh clone's root, with
      `dir` an existing directory outside the clone:
      ```bash
      dir="<dir>"
      lead='[[:space:]]*((#|//|/\*|\*|--|;|%|<!--|\.\.)[[:space:]]*)?'
      lead="${lead}SPDX-License-Identifier:"
      { echo "-- stage names"
      python3 -c 'import json; print(*sorted(r["stage_name"] for r in
          json.load(open("ci/pin-set.json"))["repos"].values()
          if r["stage_name"]))'
      for t in staging/*/; do (
          n=$(basename "$t")
          row=$(grep -E "^\| $n \|" NOTICE.md | cut -d'|' -f3 |
              sed 's/`//g; s/^ *//; s/ *$//')
          echo "== $n (row: ${row:-NONE})"
          cd "$t" || { echo "UNREADABLE"; exit 1; }
          echo "-- license and provenance files, at any depth"
          find . -path ./.git -prune -o -type f \( -iname 'LICEN[CS]E*' \
              -o -iname 'COPYING*' -o -iname 'PROVENANCE*' \) -print |
              sort | while read -r f; do echo "--- $f"; head -n 3 "$f"; done
          [ -d LICENSES ] && echo "-- LICENSES/" && ls LICENSES
          grep -rnIE --exclude-dir=.git --exclude='*.license' "^$lead" . |
              sort | awk -v row="${row:-NONE}" '{
                  id = $0; sub(/.*SPDX-License-Identifier:[ \t]*/, "", id)
                  sub(/[ \t]*(\*\/|-->)?[ \t]*$/, "", id)
                  n[id]++; if (id != row) other = other $0 "\n" }
                  END { print "-- comment-led SPDX lines, per license"
                        for (i in n) print n[i], i | "sort -k2"
                        close("sort -k2")
                        print "-- comment-led SPDX lines, other than the row"
                        printf "%s", other }'
          echo "-- other SPDX mentions (strings, JSON \$comment, prose)"
          grep -rnI --exclude-dir=.git --exclude='*.license' \
              --exclude=REUSE.toml "SPDX-License-Identifier" . |
              grep -vE "^\./[^:]*:[0-9]+:$lead" | sort
          echo "-- REUSE files"
          find . -path ./.git -prune -o \
              \( -name REUSE.toml -o -path '*/.reuse/dep5' \) -print |
              sort | while read -r f; do echo "--- $f"; cat "$f"; done
          echo "-- .license sidecars"
          find . -path ./.git -prune -o -name '*.license' -print |
              sort | while read -r f; do
                  echo "--- $f"; grep "SPDX-License-Identifier" "$f"
              done
      ); done; } > "$dir/staged-licenses.txt"
      ```
      It passes when all of this holds; if not, delete the local tag, fix on
      `dev`, and start again:
      - the file has one `==` section per stage name it starts with (from
        `ci/pin-set.json`), none `UNREADABLE` and none with row `NONE`;
      - the root `LICENSE` or `COPYING` is the row license's text, and
        `LICENSES/`, where present, holds no license other than the row's
        and the exceptions';
      - every file the listing gives another license (a comment-led line
        other than the row's, a JSON `$comment`, a REUSE entry, a sidecar)
        is named in the row's Exceptions under that same license, and so is
        the directory of every nested license file whose license is not the
        row's; a directory exception covers every file below it;
      - every exception the row names is backed in the listing under the
        license the row gives it, or, where only prose states it (a README
        license section, a `PROVENANCE.md`, a `LICENSE.upstream`, as for
        interposer-pnr's `tests/data/frozen/`, named in its README and
        `MANIFEST.json`), in that file, read by hand;
      - a JSON `$comment` is read as its file's license. Any other line can
        be a template the tree writes out, even a comment-led one inside a
        multi-line string: open the file whenever a line's license differs
        from the row.
- [ ] Only when the license check above passed on this image's `staging/`,
      take this image as the release's reference image: the adk-examples
      release lock names its ID
      (`docker image inspect --format '{{.Id}}' adk-tools:dev`), the sha256
      of this commit's `ci/pin-set.json` and this commit, the adk-tools the
      examples go with (adk-examples `docs/RELEASE_LOCK.md`). No image goes out
      with the release; a user's own build of the tag runs against that lock
      by its pin set (`docs/RUN_SH.md`).
- [ ] The VS Code extension is built from packaging-flow at the release tag,
      not handed out: `vscode/scripts/build-vsix.sh` in that checkout writes
      `vscode/dist/adk-packaging-<version>.vsix` and the `SHA256SUMS`
      `bin/adk-vscode --vsix` checks it against. Build it once from the tag
      and install it with `bin/adk-vscode --vsix` to confirm the step
      adk-examples' `two_die_echo_shifted_gf180/AUTOMATION.md` gives works.
- [ ] Push: `git push origin main && git push origin vYYYY.MM`. The ruleset
      takes changes to `main` only through a pull request
      (`CONTRIBUTING.md`); this push goes through the maintainers' bypass, so
      push exactly the commit you built and checked, with the `NOTICE.md`
      the license check read. The tag is distribution: no tag goes out from
      pins that fail the license check.
- [ ] Merge any post-release fixes back into `dev`.

---

## Known limits

- **The build context is not the git tree.** `.dockerignore` is an exclude
  list over the working tree, so files that are untracked or git-ignored under
  `tools/` still enter the context and get baked into the image. `release.sh`'s
  provenance gate reads `git status` and `git submodule status`, so it cannot
  see them: an image built in a working checkout can differ from one built from
  a clean clone. Build a release image from a fresh clone of the checkout.
- `kicad-symbols` and `kicad-footprints` are pinned by the mutable `9.0.9.1`
  tag, not by commit.
- The build needs github.com and gitlab.com egress.
