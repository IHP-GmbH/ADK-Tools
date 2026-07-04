# Chiplet source and gds2kicad prior steps

The KiCad library the `two_die_interposer` board consumes,
`../kicad/two_die_interposer.pretty/*.kicad_mod` and
`../kicad/two_die_interposer.kicad_sym`, is not hand-drawn: it is produced from
the chiplet layouts with the gds2kicad tools before any board work begins. This
directory ships those inputs so the prior steps are reproducible, and the verify
build gates them.

## Inputs shipped here

- `Metal_Test.gds` - the die layout used for both dies (IHP SG13G2; the I/O pads
  live on TopMetal2, GDS layer `134/0`, with pin names on the text layer `134/25`).
- `metal_test_chiplet.pins.json` - the curated 58-pad pin list (names + geometry)
  for the die. It is the captured result of the pad-review curation in step 1 and
  can be re-extracted from the committed footprint with `footprint_to_pinlist.py`.

The I/O pad chiplet (`interposer_bondpad`) has no GDS: it is generated
parametrically (step 3).

The commands below use the gds2kicad tool tree at `${GDS_TO_KICAD_ROOT}`
(`/opt/adk-tools/gds_to_kicad` in the image) and its bundled `pdks/sg13g2.lyp`.

## 1. metal_test_chiplet footprint (58 pads) - human-in-the-loop

`Metal_Test.gds` exposes ~474 shapes on TopMetal2; only 58 are external I/O
pads. Those are selected with the pad-review workflow, not a one-shot convert:

```bash
# 1a. emit all pad-layer shapes for review
gds_to_kicad.py Metal_Test.gds --lyp-file $GDS_TO_KICAD_ROOT/pdks/sg13g2.lyp \
    --layer TopMetal2.drawing --flip-chip --generate-pad-review review.gds
# 1b. open review.gds in KLayout, delete every shape that is not an I/O pad, save
# 1c. build the footprint from the curated pads, names from the pin list
gds_to_kicad.py --from-pad-review review.gds \
    --pin-list metal_test_chiplet.pins.json \
    --layer TopMetal2.drawing --lyp-file $GDS_TO_KICAD_ROOT/pdks/sg13g2.lyp \
    --flip-chip -o metal_test_chiplet.kicad_mod
```

## 2. Metal_Test symbol (58 pins) - reproducible, no GDS

```bash
gds_to_kicad_symbol.py --from-pin-list metal_test_chiplet.pins.json \
    -o Metal_Test.kicad_sym
```

## 3. interposer_bondpad footprint + IOPad_WireBond_100x100 symbol - parametric

```bash
$GDS_TO_KICAD_ROOT/io_pads/generate_io_pad.py --io-class wire_bond --size 100x100
# then rename the footprint to interposer_bondpad to match the board's fp-lib nickname
```

The three symbols are assembled into `two_die_interposer.kicad_sym`; the two
footprints go into `two_die_interposer.pretty/`.

## Verified in CI

The verify build's `gds2kicad prior steps` stage reruns steps 2 and 3 and diffs
them against the committed library, checks that
`metal_test_chiplet.pins.json` still matches the committed footprint, and runs
step 1's converter on `Metal_Test.gds` as a liveness check. See
`bin/adk-verify-chiplet-artifacts`.
