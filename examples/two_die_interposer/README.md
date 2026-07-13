# two_die_interposer

Reference heterogeneous-design project for the adk-tools flow: two dies mounted
on an IntM4TM2 interposer, connected through cu-pillar / micro-bump interconnect
(2.5D), with wire-bond I/O pads at the interposer edge. It doubles as the
template that `adk-new-project` scaffolds, so its layout is the recommended one
for any new design.

## Layout

```
two_die_interposer/
  kicad/       KiCad source (the human-authored input)
    two_die_interposer.kicad_pro / .kicad_sch / .kicad_pcb
    two_die_interposer.kicad_sym    (project symbols, produced by gds2kicad)
    two_die_interposer.pretty/      (project footprints, produced by gds2kicad)
    sym-lib-table / fp-lib-table    (register those two libraries with the project)
  chiplets/    gds2kicad prior-steps inputs (see chiplets/README.md)
    Metal_Test.gds                 die layout shared by both dies
    metal_test_chiplet.pins.json   curated 58-pad pin list
  outputs/     export products written by the Chiplet Export plugin
    two_die_interposer.chiplet            chiplet-studio entry point
    MANIFEST.md                           index of the products
    layout/    GDS layouts + their sidecars
      two_die_interposer_interposer.gds   referenced by the .chiplet
      *.boundaries.json / *.ixn_methods.json  assembly-DRC sidecars
    reports/   DRC reports: *_assembly_drc.lyrdb, *_cupillar_drc.json
    (regenerated, git-ignored: layout/_complete.gds, .hyp, reports/assembly_drc/, logs/)
```

The `.chiplet` references its interposer GDS by a `layout/<file>` relative path,
which chiplet-studio resolves against the `.chiplet`'s own directory; it then
auto-detects the `_complete.gds` from that same `layout/` dir, and the assembly
DRC finds each `*.boundaries.json` next to its GDS. So the GDS layouts and their
sidecars travel together under `layout/`, the DRC reports under `reports/`, and
only the `.chiplet` + `MANIFEST.md` sit at the `outputs/` root. Each die's own
layout is `chiplets/Metal_Test.gds`, shipped with the example: the footprint (and
the `.chiplet` it produces) reference it board-relative as
`../chiplets/Metal_Test.gds`, so the export and chiplet-studio resolve the die
layout from the example itself, not from any external tool tree. This is the same
die that seeds the reproducible prior steps below.

## Prior steps: chiplet footprints and symbols (gds2kicad)

The KiCad library this board is built on (`kicad/two_die_interposer.pretty/*` and
`kicad/two_die_interposer.kicad_sym`) is produced from the chiplet layouts with
the gds2kicad tools, before any board work starts. Those inputs and the exact
recipe live in `chiplets/` (see `chiplets/README.md`). The verify build reruns
the reproducible steps and gates them: the die symbol and the I/O pad are
regenerated and diffed against the committed library, the pin list is checked
against the committed footprint, and the footprint converter is run on
`Metal_Test.gds` as a liveness check.

## Open it

```bash
chiplet-studio example/outputs/two_die_interposer.chiplet
kicad          example/kicad/two_die_interposer.kicad_pro
gds-to-kicad   # unified symbol + footprint GUI; load chiplets/Metal_Test.gds to reproduce the die library
```

## Regenerate (outputs)

The verify build regenerates `outputs/` from `kicad/` on every image build and
gates it through the assembly DRC. To do it by hand:

```bash
python3 /opt/adk-tools/chiplet_kicad_plugin/tests/regenerate_wirebond_demo.py \
    --require-drc \
    --board   example/kicad/two_die_interposer.kicad_pcb \
    --output-dir example/outputs
```

`adk-smoke` runs the same export end-to-end into a throwaway dir.
