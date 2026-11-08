#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# Checks an OpenSTA `sta` for the guard in
# patches/opensta-timing-model-clocked-output-exists.patch. A timing model
# written by write_timing_model must carry, for each clock edge that reaches an
# output, a delay table only for the output transitions some path reaches. A
# forwarded clock (a buffer or an inverter from the clock port to an output)
# reaches the output with one transition per edge; a flop output with both.
# Without the guard the other transition's table is written anyway, from memory
# no path set, so this test fails on an unpatched sta: that is its ablation.
#
# Models are written with glibc's MALLOC_PERTURB_=165, which fills fresh
# allocations, so a value read from unset memory shows as 1.5365e25 ns.
#
# Needs bash, awk and coreutils. Writes only under one mktemp directory.
#
# Usage: tests/nix/test-opensta-timing-model-guard.sh <path to sta>

set -uo pipefail
sta="${1:?usage: $0 <path to sta>}"
[ -x "$sta" ] || {
    echo "not executable: $sta" >&2
    exit 2
}
case "$sta" in /*) ;; *) sta="$PWD/$sta" ;; esac

work="$(mktemp -d "${TMPDIR:-/tmp}/adk-sta-guard-test.XXXXXX")"
trap 'rm -rf "$work"' EXIT
cd "$work" || exit 2

cat > cells.lib <<'EOF'
library (cells) {
  delay_model : table_lookup;
  time_unit : "1ns";
  voltage_unit : "1V";
  current_unit : "1mA";
  capacitive_load_unit (1, pf);
  pulling_resistance_unit : "1kohm";
  leakage_power_unit : "1nW";
  input_threshold_pct_rise : 50; input_threshold_pct_fall : 50;
  output_threshold_pct_rise : 50; output_threshold_pct_fall : 50;
  slew_lower_threshold_pct_rise : 20; slew_upper_threshold_pct_rise : 80;
  slew_lower_threshold_pct_fall : 20; slew_upper_threshold_pct_fall : 80;
  nom_process : 1; nom_voltage : 1.2; nom_temperature : 25;
  lu_table_template (t1) { variable_1 : total_output_net_capacitance; index_1 ("0.001, 0.1"); }
  cell (BUF) {
    area : 1;
    pin (A) { direction : input; capacitance : 0.002; }
    pin (X) { direction : output; function : "A";
      timing () { related_pin : "A"; timing_sense : positive_unate;
        cell_rise (t1) { values ("0.10, 0.30"); } cell_fall (t1) { values ("0.12, 0.32"); }
        rise_transition (t1) { values ("0.02, 0.20"); } fall_transition (t1) { values ("0.02, 0.20"); } } }
  }
  cell (INV) {
    area : 1;
    pin (A) { direction : input; capacitance : 0.002; }
    pin (Y) { direction : output; function : "!A";
      timing () { related_pin : "A"; timing_sense : negative_unate;
        cell_rise (t1) { values ("0.08, 0.28"); } cell_fall (t1) { values ("0.09, 0.29"); }
        rise_transition (t1) { values ("0.02, 0.20"); } fall_transition (t1) { values ("0.02, 0.20"); } } }
  }
  cell (DFF) {
    area : 4;
    ff (IQ, IQN) { clocked_on : "CLK"; next_state : "D"; }
    pin (CLK) { direction : input; clock : true; capacitance : 0.002; }
    pin (D) { direction : input; capacitance : 0.002;
      timing () { related_pin : "CLK"; timing_type : setup_rising;
        rise_constraint (scalar) { values ("0.05"); } fall_constraint (scalar) { values ("0.05"); } }
      timing () { related_pin : "CLK"; timing_type : hold_rising;
        rise_constraint (scalar) { values ("0.01"); } fall_constraint (scalar) { values ("0.01"); } } }
    pin (Q) { direction : output; function : "IQ";
      timing () { related_pin : "CLK"; timing_type : rising_edge;
        cell_rise (t1) { values ("0.20, 0.40"); } cell_fall (t1) { values ("0.22, 0.42"); }
        rise_transition (t1) { values ("0.02, 0.20"); } fall_transition (t1) { values ("0.02, 0.20"); } } }
  }
}
EOF
for g in BUF:X INV:Y; do
    cat > "top_${g%%:*}.v" <<EOF
module top (CLK, D, Q, fclk);
  input CLK, D;
  output Q, fclk;
  ${g%%:*} g0 (.A(CLK), .${g##*:}(fclk));
  DFF f0 (.CLK(CLK), .D(D), .Q(Q));
endmodule
EOF
done
cat > nogen.sdc <<'EOF'
create_clock -name CLK -period 20.0 [get_ports CLK]
set_propagated_clock [all_clocks]
set_input_delay -clock [get_clocks CLK] 2.0 [get_ports D]
set_output_delay -clock [get_clocks CLK] 2.0 [get_ports Q]
set_load 0.05 [get_ports {Q fclk}]
set_input_transition 0.1 [get_ports {CLK D}]
EOF
# The generated clock on the output port, as a source-synchronous interface declares it.
sed -e '1a create_generated_clock -name fclk -source [get_ports CLK] -divide_by 1 [get_ports fclk]' \
    -e 's/-clock \[get_clocks CLK\] 2.0 \[get_ports Q\]/-clock [get_clocks fclk] 2.0 [get_ports Q]/' \
    nogen.sdc > gen.sdc

fails=0
ok() { echo "ok   $*"; }
bad() { echo "FAIL $*"; fails=$((fails + 1)); }

# model <case> <cell> <sdc>: writes <case>.lib, and reads it back.
model() {
    cat > "$1.tcl" <<EOF
read_liberty cells.lib
read_verilog top_$2.v
link_design top
read_sdc $3
set_clock_latency -source 0 [all_clocks]
write_timing_model -library_name $1 -cell_name top $1.lib
EOF
    printf 'read_liberty %s.lib\n' "$1" > "$1_read.tcl"
    if MALLOC_PERTURB_=165 timeout 60 "$sta" -no_splash -exit "$1.tcl" > "$1.log" 2>&1 &&
        [ -s "$1.lib" ]; then
        ok "$1: model written"
    else
        bad "$1: no model written (see below)"
        cat "$1.log"
        return 1
    fi
    if timeout 60 "$sta" -no_splash -exit "$1_read.tcl" > "$1_read.log" 2>&1 &&
        ! grep -qi 'error' "$1_read.log"; then
        ok "$1: model reads back"
    else
        bad "$1: model does not read back"
        cat "$1_read.log"
    fi
    if grep -q '15365222393725398659629056' "$1.lib"; then
        bad "$1: a table holds the allocator's fill"
    else
        ok "$1: no table holds the allocator's fill"
    fi
    # Every table entry a finite number under 1 us: nothing here is slower than 20 ns.
    if awk '/values\(/ { v = $0; sub(/.*values\("/, "", v); sub(/"\).*/, "", v)
                         n = split(v, a, ","); for (i = 1; i <= n; i++) {
                           if (a[i] !~ /^ *-?[0-9]+(\.[0-9]+)? *$/ || a[i] + 0 > 1000 || a[i] + 0 < -1000) bad = 1 } }
            END { exit bad }' "$1.lib"; then
        ok "$1: every table entry is finite and under 1 us"
    else
        bad "$1: a table entry is not finite or not under 1 us"
    fi
}

# tables <model> <pin>: one line per table of the pin, "related type table values".
tables() {
    awk -v want="$2" '
        /^[ \t]*pin\(/ { p = $0; sub(/.*pin\("?/, "", p); sub(/"?\).*/, "", p); inpin = (p == want); next }
        !inpin { next }
        /timing\(\)/ { rel = "-"; typ = "-" }
        /related_pin/ { rel = $0; sub(/.*: *"/, "", rel); sub(/".*/, "", rel) }
        /timing_type/ { typ = $0; sub(/.*: */, "", typ); sub(/;.*/, "", typ) }
        /^[ \t]*(cell_rise|cell_fall|rise_transition|fall_transition)\(/ {
            tb = $0; sub(/^[ \t]*/, "", tb); sub(/\(.*/, "", tb) }
        /values\(/ { v = $0; sub(/.*values\("/, "", v); sub(/"\).*/, "", v); print rel, typ, tb, v }
    ' "$1" | LC_ALL=C sort
}

# arcs <case> <pin> <expected "related type table" lines>: the exact set of tables.
arcs() {
    local got want
    got="$(tables "$1.lib" "$2" | cut -d' ' -f1-3)"
    want="$(printf '%s\n' "$3" | LC_ALL=C sort)"
    if [ "$got" = "$want" ]; then
        ok "$1: $2 carries exactly the reached transitions"
    else
        bad "$1: $2 tables differ from the reached transitions"
        diff <(echo "$want") <(echo "$got") | sed 's/^/     /'
    fi
}

# value <case> <pin> <related type table> <values>
value() {
    local got
    got="$(tables "$1.lib" "$2" | awk -v k="$3" '$1" "$2" "$3 == k { print $4 }')"
    if [ "$got" = "$4" ]; then
        ok "$1: $2 $3 is $4"
    else
        bad "$1: $2 $3 is '$got', expected $4"
    fi
}

# Forwarded clock through a buffer: a rising edge reaches fclk rising only, a falling edge falling only.
reach_buf="CLK rising_edge cell_rise
CLK rising_edge rise_transition
CLK falling_edge cell_fall
CLK falling_edge fall_transition"
# Through an inverter: the other way round.
reach_inv="CLK rising_edge cell_fall
CLK rising_edge fall_transition
CLK falling_edge cell_rise
CLK falling_edge rise_transition"
# A flop output reaches both transitions from the one edge that clocks it.
reach_q="CLK rising_edge cell_fall
CLK rising_edge cell_rise
CLK rising_edge fall_transition
CLK rising_edge rise_transition"

if model buf_nogen BUF nogen.sdc; then
    arcs buf_nogen fclk "$reach_buf"
    value buf_nogen fclk "CLK rising_edge cell_rise" "0.10000,0.30000"
    arcs buf_nogen Q "$reach_q"
    value buf_nogen Q "CLK rising_edge cell_rise" "0.20000,0.40000"
    value buf_nogen Q "CLK rising_edge cell_fall" "0.22000,0.42000"
fi
if model inv_nogen INV nogen.sdc; then
    arcs inv_nogen fclk "$reach_inv"
    value inv_nogen fclk "CLK rising_edge cell_fall" "0.09000,0.29000"
    arcs inv_nogen Q "$reach_q"
fi
if model buf_gen BUF gen.sdc; then
    # The generated clock adds the same arcs related to fclk itself.
    arcs buf_gen fclk "$reach_buf
${reach_buf//CLK/fclk}"
    value buf_gen fclk "CLK rising_edge cell_rise" "0.10000,0.30000"
    value buf_gen fclk "fclk rising_edge cell_rise" "0.10000,0.30000"
    arcs buf_gen Q "$reach_q"
fi

if [ "$fails" -eq 0 ]; then
    echo "PASS"
    exit 0
fi
echo "FAILED: $fails check(s)"
exit 1
