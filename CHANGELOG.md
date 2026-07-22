# Changelog

All notable changes to the `adk-tools` image. The format is based on
[Keep a Changelog](https://keepachangelog.com/); versioning is calendar-based
(`vYYYY.MM`). Each entry describes a pinned, verify-gated combination of the
bundled tools; the exact submodule pins for a tag are in that commit's
`git submodule status` and in the baked `manifest.json`.

## [Unreleased]

_Development happens on `dev`; entries accumulate here until the next release._

## [v2026.07] - 2026-07-22

First public-candidate release. Re-cut of the unpublished 2026-07-13 internal
v2026.07 snapshot (folded in below) to add the demo-reproducibility,
DRC-correctness and die-GDS portability work that landed on `dev` since. This is
the tag the first public repos + registry publish from; see
`docs/RELEASE_CHECKLIST.md` part B for the remaining go-public steps.

### Changed
- Chiplet export GUI: the per-die silicon-body thickness field moved out of the
  interconnect-method row into a collapsed **3D / advanced** pane. It is a
  3D-only quantity (`dimensions.thickness`, for the 3Dblox export and the
  render) with no effect on the 2D GDS, the footprint, the placement or the DRC,
  so next to the method dropdown it misread as "the thickness of that
  connection"; it is still pre-filled from the board's `DIE_THICKNESS_UM` field.
  The export `MANIFEST.md` now also lists the cu-pillar `*.pillars.json`
  sidecars.
- New reproducibility verify gate. `adk-verify-demo-reproducible` regenerates
  the `two_die_interposer` demo headless during the build and fails if the
  shipped `outputs/` no longer matches a fresh regen -- die geometry, pillar
  manifests, boundary geometry, and the interposer GDS (compared by a
  GDSII-timestamp-normalized geometry digest rather than skipped). Closes the
  hole that let a stale or GUI-divergent demo ship on a green build.
- Release tooling hardening (no image-behavior change). `release.sh` now requires
  an immutable `vYYYY.MM` tag, refuses to overwrite an already-published tag,
  never prunes published digests by default (opt-in `PRUNE_UNTAGGED=1` for the
  private-package quota), and gates the push on a clean tree built from the
  tagged commit (working tree + submodule pins + `HEAD == tag` + the baked
  manifest `meta` matching the tag). `.dockerignore` stops baking git-ignored
  `gds_to_kicad/tests/test_*.gds` scratch and `SESSION_LOG.md` into the image.
  Docs (`RELEASE_CHECKLIST.md`, `MAINTAINING.md`) align the publish command to
  `vYYYY.MM`, check off the `release.sh` redesign, and record the pre-public
  git-history scrub as a decided step to run immediately before the first public
  push.
- Completed the `attachment_surface_z` rollout and re-synced the three tools that
  advanced past the previous pins: `chiplet-studio` -> `e124c0f`,
  `chiplet_kicad_plugin` -> `24d2622`, `OpenIntM4TM2` -> `5e58cde`. The
  interposer's die-attachment surface is now carried as the component-level
  `attachment_surface_z` (13.83 um, emitted by the plugin's `hyp_to_gds` update
  pass and read by the ADK exporter and Chiplet Studio), decoupled from
  `dimensions.thickness`, which is now the interposer physical body. This
  required the paired demo fix that had been missing when the feature first
  landed: the `two_die_interposer` example board's physical thickness dropped
  from the KiCad default 1.56 mm to a self-consistent 300 um stackup (coppers
  15 um, prepregs 50 um, core 140 um; `general` == stackup sum == 0.300 mm), so
  the regenerated demo `.chiplet` reports `dimensions.thickness = 300` and
  satisfies Chiplet Studio's `CoordFrameContractWirebondDemo` contract test (the
  flip-chip dies are unaffected -- they mount on `attachment_surface_z`).
  `OpenIntM4TM2` also adds IntM4TM2 EM/extraction technology stackups
  (openEMS/palace/parasitics workflows) and an LVS testing harness, runs all four
  via4 enclosure DRC rules on the seal-excluded layers, and migrated its
  Cu-pillar/LVS harnesses into the `testing/` convention; its `intm4tm2_tests`
  gate stays green (201 passed, 13 skipped). All ten image gates green: studio
  ctest 583/583, plugin 238 passed, adk-smoke PASS.
- Coordinated a frame-contract hardening across the ADK, Chiplet Studio and the
  KiCad plugin, closing findings reported against the die coordinate frame
  (`chiplet-spec/coord_frame_contract.md`). `adk` -> `16c9557`: the two `.chiplet`
  consumers (`checks/pads_vs_pillars.py`, `openroad/chiplet2dbx.py`) now validate
  each die's `anchor:` -- only `gds_origin` is supported; `bbox_center`, an
  unknown value, or an absent `anchor:` is a hard error instead of a silent
  ~82 um bbox-corner misplacement that could flip a matched net to open (absent
  is rejected because the contract defaults it to `bbox_center`, which these
  tools cannot consume). `pads_vs_pillars` also guards its `rotation.z` parse and
  runs its whole pipeline inside one exit-code guard, so a malformed input can no
  longer leak to exit 1 and collide with the findings tier. `chiplet-studio` ->
  `206df2e`: the reader stops silently aliasing `face_down` and silently dropping
  unknown orientation tokens to an un-mirrored `face_up`; it now warns (a lenient
  viewer, never silent), geometry unchanged. `chiplet_kicad_plugin` -> `cd43520`:
  both the `.chiplet` writer and `hyp_to_gds` reject a non-canonical `ORIENTATION`
  token (a typo or `face_down`) rather than emit an un-mirrored die; also picks
  up an unrelated export-dialog usability commit already on the plugin's main.
  The frame contract itself gained a section defining the orientation vocabulary
  as only `face_up`/`flip_chip` (chiplet-spec, separate repo). All three tools
  reject the non-canonical `face_down` token pointing at `flip_chip`.
- `OpenIntM4TM2` pin advanced to `967b351` (past its disclaimer README commit),
  adding PDK-internal device libraries with no change to the assembly path. Two
  additions: the interposer bondpad/probepad PCell (`bondpad_code.py`, registered
  in `intm4tm2_pycell_lib` alongside `CuPillarPad`/`cmim`, its geometry keyed by
  new `bondpad_*` and via/metal parameters in `intm4tm2_tech.json`), and a KiCad
  `cap_cmim` symbol with a capacitance-keyed CMIM footprint family
  (`CMIM_10fF`..`CMIM_5pF`, generated by `libs.tech/kicad/scripts/cmim_footprint_gen.py`).
  The image's `intm4tm2_tests` gate exercises the new bondpad PCell and the
  cmim footprint/symbol fidelity checks and stays green (167 passed, 13 skipped).
  The `CuPillarPad` PCell, the layer map, the DRC decks and the KLayout tech are
  untouched, so the plugin's assembly export and its DRC are unaffected.
- Added a preview-release status disclaimer -- a `## Status` GitHub Warning
  callout stating the project is currently a preview release only -- to the
  `adk-tools` README and each of the six bundled tools' READMEs
  (`chiplet-studio`, `chiplet_kicad_plugin`, `gds_to_kicad`, `adk`,
  `OpenIntM4TM2`, `IHP-Interconnect-IntM4TM2`). Docs-only: each tool pin advanced
  by exactly one README commit over its prior pin. The `kicad` upstream fork is
  intentionally excluded.
- Coordinated the interposer PDK + plugin advance across two pins.
  `OpenIntM4TM2` advanced to `50bb458`: the 158-entry layer-map parity migration
  (`prBoundary` moved `189/0` -> `235/0`, `MEMVia`/`RFMEM` removed, plus the
  ported SG13G2 BEOL DRC decks with unit testcases), and on top of it the new
  IntM4TM2 PCell library (`CuPillarPad`) and the `cmim` MIM-cap device stack
  (PCell, ngspice model, LVS extraction, MIM DRC table, xschem symbol). The
  image's verify stage runs the PDK's `intm4tm2_tests`, which stay green in the
  image's `KLAYOUT_PATH`-registered environment with no image-side shim: the PDK
  isolated its Cu-pillar PCell parity test from a preloaded technology and ported
  `run_lvs.py` to stdlib `argparse` (no `docopt` dependency).
  `chiplet_kicad_plugin` advanced to `b69de14`: it emits the die board outline on
  the new `prBoundary 235/0` (keeping `189/0` in the boundary-viz collision
  guard), resolves a board-relative die `GDS_FILE` against the `.hyp`
  `{BOARD ...}` directory (relied on by the self-contained `two_die_interposer`
  example), and now merges the generated Cu-pillar cells by iterating
  `Layout.each_cell()` instead of `range(cells())`. The new PCell-based Cu-pillar
  generator flattens its variant and prunes the leftover proxy cell, leaving a
  freed cell-index slot that the old `cell(ci)` loop hit with "Not a valid cell
  index", aborting the assembly export; `each_cell()` skips the gap. The
  example's committed interposer output
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
- Made the shipped `two_die_interposer` demo reproducible and DRC-clean, and
  fixed the die layout not rendering in the viewer -- three independent causes.
  (1) Die width/height was non-deterministic between the KiCad GUI and a
  headless export: the writer read a lazily-built courtyard cache the GUI keeps
  warm but a headless `LoadBoard` leaves empty, silently falling back to a
  text-inflated bounding box (801x3359 um vs the real courtyard 770x2606 um).
  `chiplet_writer` now builds the courtyard caches before reading them (and
  warns instead of swallowing a build failure); a determinism test locks
  cold==warm. The die's physical thickness is taken from a board
  `DIE_THICKNESS_UM` field (750 um on U1/U2) so it no longer depends on GUI
  input. (2) The cu-pillar DRC checked `vendorx_microbump` (body 40 um, absent
  from the IHP Table 6.1 map) against the Option-2 default 80 um pitch instead
  of the vendor's declared 50 um, turning the die's clean native 70 um bump
  pitch into a phantom 72 nm violation (auto-resolve spread it toward 80 and
  fell short); `hyp_to_gds` now takes pitch/spacing from the method's manifest
  `pitch_rules`, so U2 is DRC-clean and the cu-pillar and assembly DRC agree on
  the same geometry (a no-op for the in-table cupillar methods). (3) The
  exported `.chiplet` referenced the die GDS by a board-relative path that
  dangled when the export landed outside the example, leaving an empty die box
  in the viewer; the die GDS is now bundled next to the output when that path is
  unreachable and left untouched when it already resolves. The demo `outputs/`
  were regenerated end-to-end (die 770x2606x750, both `*.pillars.json`,
  cu-pillar DRC clean).
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

## v2026.07 — 2026-07-13 internal snapshot (unpublished; folded into the release above)

The first cut under the `main` = stable / `dev` = development model. Tagged
locally only and never published to the registry; its `v2026.07` tag was re-cut
at the 2026-07-22 commit above, which supersedes this snapshot. Notes retained
for history:

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
