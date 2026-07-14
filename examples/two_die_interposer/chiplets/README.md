# Chiplet source and gds2kicad prior steps

The KiCad library the `two_die_interposer` board consumes,
`../kicad/two_die_interposer.pretty/*.kicad_mod` and
`../kicad/two_die_interposer.kicad_sym`, is not hand-drawn: it is produced from
the chiplet layouts with the gds2kicad tools before any board work begins. This
directory ships every input needed to reproduce those prior steps end-to-end
with no manual work, and the verify build gates the reproduction.

## Inputs shipped here

- `Metal_Test.gds` - the full die layout used for both dies (IHP SG13G2; the I/O
  pads live on TopMetal2, GDS layer `134/0`, with pin names on the text layer
  `134/25`). It is the canonical die layout: the footprint links to it
  board-relative (`../chiplets/Metal_Test.gds`), so the assembly export and
  chiplet-studio read the die geometry straight from here, not from any external
  tool tree. The names on `134/25` are authoritative for the pins -- e.g. the two
  power pads are labelled `VCC`, so the footprint/symbol/board use `VCC` too.
- `Metal_Test_stripped.gds` - the curated die layout: only the 58 external I/O
  pads on `134/0`, each with its name on `134/25`. This is the frozen result of
  the human-in-the-loop pad review (the full die exposes ~474 TopMetal2 shapes;
  only 58 are I/O pads). Shipping it turns the footprint -- otherwise the one
  hand-curated artifact -- into a one-shot reproducible convert (step 1).
- `metal_test_chiplet.pins.json` - the 58-pad pin list (names + geometry) for the
  die, used to build the symbol (step 2). It can also be re-extracted from the
  committed footprint with `footprint_to_pinlist.py`.
- `metal_test_chiplet.g2kproj` - a saved gds-to-kicad GUI session bundling the
  above: the input + stripped GDS, the LYP, the pad/text layer selections and the
  embedded pin list. Open it with `gds-to-kicad` -> File -> Open Project to land
  at the exact state that produces the footprint + symbol, no re-entry needed. It
  stores image-absolute paths (`/opt/adk-tools/...`), so it is meant for use
  inside the adk-tools container.

The I/O pad chiplet (`interposer_bondpad`) has no GDS: it is generated
parametrically (step 3).

The commands below use the gds2kicad tool tree at `${GDS_TO_KICAD_ROOT}`
(`/opt/adk-tools/gds_to_kicad` in the image) and its bundled `pdks/sg13g2.lyp`.

## 1. metal_test_chiplet footprint (58 pads) - reproduced from the stripped GDS

With the curated `Metal_Test_stripped.gds` shipped, the footprint is a one-shot
convert: read the 58 pads from `134/0`, take their names from `134/25`.

```bash
gds_to_kicad.py Metal_Test_stripped.gds \
    --layer TopMetal2.drawing --text-layer-number 134/25 \
    --lyp-file $GDS_TO_KICAD_ROOT/pdks/sg13g2.lyp --flip-chip \
    -o metal_test_chiplet.kicad_mod
```

This regenerates the committed footprint's pad set exactly (names + geometry);
the verify build asserts that equality (see below).

### How the stripped GDS was curated (provenance)

`Metal_Test_stripped.gds` was produced once, by hand, from the full die GDS. To
redo the curation from scratch (e.g. for a different die):

```bash
# 1a. emit all pad-layer shapes for review
gds_to_kicad.py Metal_Test.gds --lyp-file $GDS_TO_KICAD_ROOT/pdks/sg13g2.lyp \
    --layer TopMetal2.drawing --flip-chip --generate-pad-review review.gds
# 1b. open review.gds in KLayout, delete every shape that is not an I/O pad, and
#     keep each pad's name text on layer 134/25 (not on the pad layer 134/0);
#     save as Metal_Test_stripped.gds
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

The verify build's `gds2kicad prior steps` stage (see
`bin/adk-verify-chiplet-artifacts`) reruns the reproducible steps against the
committed library:

- step 1's convert on the shipped `Metal_Test_stripped.gds` must reproduce the
  committed footprint's pad set exactly (names + geometry) -- **strict**;
- step 2's symbol regenerates from `metal_test_chiplet.pins.json`, diffed against
  the committed `.kicad_sym` -- strict;
- step 3's io pad regenerates via `generate_io_pad`, geometry diffed -- strict;
- `metal_test_chiplet.pins.json` still matches the committed footprint -- strict;
- the converter still runs on the full `Metal_Test.gds` (474 raw shapes) -- smoke;
- `metal_test_chiplet.g2kproj` has the right format + layer selections and an
  embedded pin list matching `metal_test_chiplet.pins.json` -- strict.
