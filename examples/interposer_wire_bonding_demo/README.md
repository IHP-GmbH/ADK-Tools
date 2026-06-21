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
    interposer_wire_bonding_demo_interposer.gds     referenced by the .chiplet
    (regenerated, git-ignored: _complete.gds, .hyp, assembly_drc/, logs/)
    (also tracked when present: the *.json sidecars, *_assembly_drc.lyrdb)
```

The `.chiplet` references its interposer GDS by a bare relative name, and
chiplet-studio auto-detects the `_complete.gds` from the same directory, so the
two GDS files and the `.chiplet` deliberately live together in `outputs/`.

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
