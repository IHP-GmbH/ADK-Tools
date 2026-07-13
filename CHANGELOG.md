# Changelog

All notable changes to the `adk-tools` image. The format is based on
[Keep a Changelog](https://keepachangelog.com/); versioning is calendar-based
(`vYYYY.MM`). Each entry describes a pinned, verify-gated combination of the
bundled tools; the exact submodule pins for a tag are in that commit's
`git submodule status` and in the baked `manifest.json`.

## [Unreleased]

_Development happens on `dev`; entries accumulate here until the next release._

## [v2026.07] - 2026-07-13

Stable milestone and the first release cut under the `main` = stable / `dev` =
development model. Internal distribution (private repos and registry); see
`docs/RELEASE_CHECKLIST.md` part B for the work required before a public
release.

### Added
- Governance and maintainer documentation: `CONTRIBUTING.md`,
  `docs/MAINTAINING.md`, `docs/RELEASE_CHECKLIST.md`, and this `CHANGELOG.md`.
- `dev` integration branch and the stable/dev/feature branching model.
- Bundled `two_die_interposer` example laid out as the `kicad/` + `outputs/`
  project template a scaffolded design uses; its gds2kicad "prior steps" are
  gated in the verify stage.
- Runtime text editors `vim` and `featherpad` for editing files under `/work`.
- Interposer KLayout technology registered in the image, so exported
  interposer/assembly GDS opens with named, colored layers.

### Changed
- Submodule pins advanced across all tools (Phase 1-6 code audits; chiplet-studio
  2D + 3D overview navigators and pillar/via rendering; chiplet_kicad_plugin I/O
  pad handling and export logging; KiCad Hyperlynx export and micrometre PCB
  units; IntM4TM2 metal DRC decks).
- `gds_to_kicad` moved to the official `IHP-GmbH/gds2kicad` upstream.
- Worker-venv dependencies exact-pinned; license texts shipped inside the image.
- `run.sh` caps the container at 20 GiB RAM with swap disabled.
- Docker image seeds KiCad config to skip the first-run wizard, curates the
  global library tables to fix the startup stall, and forces software GL
  rendering for stable GUIs over remote X.
- `NOTICE.md` license accuracy: KLayout corrected to `GPL-2.0-or-later`;
  `pycell4klayout-api` (`GPL-3.0-only`) and `pypreprocessor` (`MIT`) given
  precise SPDX; the apt-installed `featherpad`/`vim` editors documented.
- `build.sh` now derives the manifest `meta` from `git describe --tags`, so an
  image self-reports its release lineage instead of a bare SHA.

### Fixed
- Removed the maintainer's absolute home path from tracked example KiCad files
  (`two_die_interposer.kicad_pcb`, `metal_test_chiplet.kicad_mod`); they now use
  the `${GDS_TO_KICAD_ROOT}` form, so no local path is baked into the image.
- Removed a leftover `chiplet-mosaic` submodule gitdir.

## [v2026.06]

Prior calendar release. See the git history up to the `v2026.06` tag for the
tool combination it pinned.
