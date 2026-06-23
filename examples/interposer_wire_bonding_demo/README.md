# interposer_wire_bonding_demo

Reference heterogeneous-design project for the adk-tools flow: a two-die
wire-bond assembly on an IntM4TM2 interposer. It doubles as the template that
`adk-new-project` scaffolds, so its layout is the recommended one for any new
design.

## Layout

```
interposer_wire_bonding_demo/
  kicad/       KiCad source (the human-authored input)
    interposer_wire_bonding_demo.kicad_pro / .kicad_sch / .kicad_pcb
    interposer_wire_bond_demo.kicad_sym
    fp-lib-table
    interposer_wire_bonding_demo.pretty/   (project footprints)
  outputs/     export products written by the Chiplet Export plugin
    interposer_wire_bonding_demo.chiplet            chiplet-studio entry point
    MANIFEST.md                                     index of the products
    layout/    GDS layouts + their sidecars
      interposer_wire_bonding_demo_interposer.gds   referenced by the .chiplet
      *.boundaries.json / *.ixn_methods.json        assembly-DRC sidecars
    reports/   DRC reports: *_assembly_drc.lyrdb, *_cupillar_drc.json
    (regenerated, git-ignored: layout/_complete.gds, .hyp, reports/assembly_drc/, logs/)
```

The `.chiplet` references its interposer GDS by a `layout/<file>` relative path,
which chiplet-studio resolves against the `.chiplet`'s own directory; it then
auto-detects the `_complete.gds` from that same `layout/` dir, and the assembly
DRC finds each `*.boundaries.json` next to its GDS. So the GDS layouts and their
sidecars travel together under `layout/`, the DRC reports under `reports/`, and
only the `.chiplet` + `MANIFEST.md` sit at the `outputs/` root.

## Open it

```bash
chiplet-studio example/outputs/interposer_wire_bonding_demo.chiplet
kicad          example/kicad/interposer_wire_bonding_demo.kicad_pro
```

## Regenerate

The verify build regenerates `outputs/` from `kicad/` on every image build and
gates it through the assembly DRC. To do it by hand:

```bash
python3 /opt/adk-tools/chiplet_kicad_plugin/tests/regenerate_wirebond_demo.py \
    --require-drc \
    --board   example/kicad/interposer_wire_bonding_demo.kicad_pcb \
    --output-dir example/outputs
```

`adk-smoke` runs the same export end-to-end into a throwaway dir.
