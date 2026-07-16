# Changelog

All notable changes to the `adk-tools` image. The format is based on
[Keep a Changelog](https://keepachangelog.com/); versioning is calendar-based
(`vYYYY.MM`). Each entry describes a pinned, verify-gated combination of the
bundled tools; the exact submodule pins for a tag are in that commit's
`git submodule status` and in the baked `manifest.json`.

## [Unreleased]

_Development happens on `dev`; entries accumulate here until the next release._

### Changed
- Coordinated the interposer BEOL layer-map parity migration across two pins:
  `OpenIntM4TM2` advanced to `2b80d07` (158-entry layer map with `prBoundary`
  moved `189/0` -> `235/0`, `MEMVia`/`RFMEM` removed, plus the ported SG13G2 BEOL
  DRC decks with unit testcases), and `chiplet_kicad_plugin` advanced to
  `4683815` (merged to the plugin's `main`) which emits the die board outline on
  the new `prBoundary 235/0` while keeping `189/0` in the boundary-viz collision
  guard. The `4683815` merge also lands the board-relative die `GDS_FILE`
  resolution (`hyp_to_gds` resolves a relative device GDS against the `.hyp`
  `{BOARD ...}` directory) already relied on by the self-contained
  `two_die_interposer` example. The example's committed interposer output
  (`outputs/layout/two_die_interposer_interposer.gds`) was regenerated so its
  board outline lands on the new `prBoundary 235/0` (it had been produced before
  the migration and still carried the outline on `189/0`); geometry is otherwise
  unchanged.
- `gds_to_kicad` pin advanced to `8a24f81` — unified-GUI usability: the "Generate
  Stripped GDS" and "Extract Pin List" steps now save through a dialog
  (user-chosen folder + file name); a whole session (input GDS/LYP/stripped-GDS
  paths, the layer selections and the edited pin list) can be saved and reopened
  as a `.g2kproj` project file; the output folder is created lazily on first
  save instead of at launch, so merely opening the GUI no longer creates
  `generated_kicad_symbol_files/`; the pin list / `.kicad_sym` / `.kicad_mod`
  export dialogs now default to the folder the tool was launched from instead of
  an internal output dir, and the footprint button is renamed for consistent
  "Export" naming. The `.g2kproj` format and the CLI writers/converters are
  unchanged.
- `adk-new-project` now scaffolds a minimal `README.md` (just the project title,
  for the author to fill in) and creates a `chiplets/` input dir alongside
  `kicad/` and `outputs/`, matching the bundled example's layout so a die GDS has
  a home before the export runs.

### Fixed
- Made the `two_die_interposer` example's symbol reproducible from the shipped
  pin list. `chiplets/metal_test_chiplet.pins.json` (and the copy embedded in
  `metal_test_chiplet.g2kproj`) had been derived from the committed footprint,
  which carries no symbol-layout semantics, so every pin defaulted to
  `side=left`/`type=passive`; regenerating the symbol -- on the CLI or via the
  GUI's "generate symbol from pin list" (both call the same layout code) --
  produced a degenerate single-column symbol instead of the committed
  multi-side one. The pin list now carries the reviewed `side`/`type` per pin
  (recovered from the committed symbol), so `gds_to_kicad_symbol.py
  --from-pin-list` and "File -> Open Project" both rebuild the committed symbol
  exactly. The verify gate's symbol step was upgraded from a pin-name-set diff
  to a full-layout diff (name + type + position), its footprint-fidelity step
  now compares only the footprint-derivable fields (name + geometry; `side`/
  `type` excluded), and the `.g2kproj` check now compares name + side + type.
- Made the `two_die_interposer` example's gds2kicad prior steps reproducible
  end-to-end from shipped inputs. Added `chiplets/Metal_Test_stripped.gds` -- the
  curated die layout (only the 58 I/O pads on `134/0`, names on `134/25`), the
  frozen result of the human-in-the-loop pad review -- so `metal_test_chiplet`'s
  footprint regenerates one-shot from it with no manual KLayout step. The verify
  gate now asserts that the footprint regenerated from the stripped GDS equals the
  committed one exactly (name + geometry, strict), upgrading the former full-GDS
  liveness smoke. Also aligned the two power pads to the die's real `134/25` label
  `VCC` (they were `Vcc`) across the footprint, symbol, pin list, board and
  schematic, so the committed library matches its own source die. Connectivity is
  unchanged (net codes keep their pad bindings; the user net stays `vcc`), and the
  assembly GDS/`.chiplet` and DRC are byte-identical since pad names do not reach
  the assembly outputs; only the two auto-generated placeholder net names for
  U2's now-renamed no-connect pads track the rename (`...PadVcc` -> `...PadVCC`).
  Also shipped `chiplets/metal_test_chiplet.g2kproj`, a saved gds-to-kicad GUI
  session (input + stripped GDS, LYP, layer selections and the embedded pin list),
  so `gds-to-kicad` -> File -> Open Project lands at the exact state that produces
  the footprint + symbol; the verify gate checks its format, layer selections and
  that its embedded pin list matches the shipped `metal_test_chiplet.pins.json`.
- Made the `two_die_interposer` example's die-GDS provenance self-contained: the
  die footprint, its PCB instances (U1/U2), and the exported `.chiplet` now
  reference the example-local `chiplets/Metal_Test.gds` board-relative
  (`../chiplets/Metal_Test.gds`) instead of the gds2kicad tool-tree copy, so the
  assembly export and chiplet-studio read the die geometry from the example
  itself. This supersedes the v2026.07 `${GDS_TO_KICAD_ROOT}` die reference; the
  `${GDS_TO_KICAD_ROOT}` form now remains only on the layer-properties `LYP_FILE`.
  Enabled by a `chiplet_kicad_plugin` fix (`hyp_to_gds`) that resolves a
  board-relative die `GDS_FILE` against the board directory (via the `.hyp`
  `{BOARD ...}` header) instead of the process CWD; absolute and `${VAR}` paths
  are unchanged, so the byte-exact writer parity is preserved.
- `gds-to-kicad` now launches the unified GUI (symbol + footprint in one window:
  Extract Pins -> Pin List Editor -> Symbol Designer -> Footprint Generator)
  instead of the footprint-only front end, so the full gds->symbol / gds->footprint
  flow is reachable from the bundled command. The `adk-tools` listing and READMEs
  are reworded to match, and the example `Open it` section points the GUI at the
  demo die GDS.
- Registered the example's symbol library with a project `sym-lib-table`
  (mirroring the existing `fp-lib-table`), so `two_die_interposer.kicad_sym` is a
  first-class, reusable project library like the footprints instead of only being
  cached inside the schematic.

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
- **All seven bundled tools pinned to their latest `main` tips**, integrated and
  verified together as a single combination (studio ctest 578/578, plugin
  pytest 211 passed, adk-smoke assembly DRC green). Final coordinated wave:
  - adk `e88eff3` — `pads_vs_pillars` manifest-level alignment check;
    `chiplet2dbx` (.chiplet -> 3Dblox exporter for OpenROAD); 0.2.0 alignment.
  - chiplet_kicad_plugin `e527459` — `<gds>.pillars.json` pillar-manifest
    sidecar; per-die physical thickness (`DIE_THICKNESS_UM`).
  - chiplet-studio `5ffe784` — OpenROAD 3Dblox flow example.
  - IHP-Interconnect-IntM4TM2 `7863100` — per-method bump LEF generator;
    manifest reader hardening.
- Earlier in the cycle, submodule pins also advanced through the Phase 1-6 code
  audits (chiplet-studio 2D + 3D overview navigators and pillar/via rendering;
  chiplet_kicad_plugin I/O pad handling and export logging; KiCad Hyperlynx
  export and micrometre PCB units; IntM4TM2 metal DRC decks).
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
