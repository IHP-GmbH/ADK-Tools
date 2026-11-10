#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# Checks staging.pin's NIX_OPENROAD_OUTPATH against the OpenROAD this flake
# builds for x86_64-linux, and NIX_LIBRELANE_OUTPATH, its interpreter's
# NIX_LIBRELANE_PYTHON_OUTPATH, NIX_SG13G2_PDK_OUTPATH,
# the lane interpreter's NIX_PYTHON_OUTPATH,
# the co-simulation engine's NIX_COSIM_IVERILOG_OUTPATH,
# NIX_COSIM_COCOTB_OUTPATH, NIX_COSIM_PYTHON_OUTPATH and
# NIX_COSIM_DEVSHELL_OUTPATH and the mixed-signal engine's
# NIX_MIXSIM_NGSPICE_OUTPATH, NIX_MIXSIM_IVERILOG_OUTPATH and
# NIX_SG13G2_OSDI_OUTPATH, the viewers' NIX_GTKWAVE_OUTPATH,
# NIX_GAW_OUTPATH and NIX_XSCHEM_OUTPATH and the Verilog-AMS engine's NIX_VAMS_GNUCAP_OUTPATH and
# NIX_VAMS_DEVSHELL_OUTPATH against its librelane, librelane-python,
# sg13g2-pdk, python, cosim-iverilog, cosim-cocotb, cosim-python,
# cosim-devshell, mixsim-ngspice, mixsim-iverilog, sg13g2-osdi, gtkwave, gaw,
# xschem, vams-gnucap and vams-devshell packages the same
# way, by evaluation alone: `nix eval` of each output path with
# import-from-derivation disabled, so evaluating it builds no derivation (flake
# inputs missing from the store are still fetched), and --max-jobs 0 besides;
# with --no-update-lock-file, so
# the committed flake.lock is what is evaluated and a lock that needs updating
# refuses instead of being rewritten. Exit 0 only when each pin is its
# package's store path. Each of these refuses, and says which pin: no pin line
# or more than one, a line that is not a double-quoted /nix/store/<hash>-<name>
# literal of that package's name, a pin file that does not source cleanly
# under set -euo pipefail (as stage-sources.sh sources it), a value a shell
# reads differently from the literal, an evaluation that fails or prints
# nothing, and a mismatch. OpenROAD is checked first, then LibreLane, its
# interpreter, the PDK, the lane's interpreter, the four co-simulation paths, the three mixed-signal paths, the two
# viewers and the two Verilog-AMS paths in that order, and the first refusal stops the check.
#
# Usage: nix/check-openroad-pin.sh [<adk-tools checkout, default this one>]

set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
ADK_NIX_LIB_ONLY=1 . "$here/cli-env.sh"

at="${1:-$(cd "$here/.." && pwd)}"
pin="$at/staging.pin"
[ -f "$pin" ] || adk_nix_die "openroad pin: $pin not found"

# check_outpath <what> <variable> <flake package> <store name, an ERE>
check_outpath() {
    local what="$1" var="$2" attr="packages.x86_64-linux.$3.outPath" name="$4"
    local count want value got
    # The line as written, not as a shell would expand it: one assignment, and
    # its value a literal with nothing in it a shell could substitute.
    count="$(grep -c -E "^[[:space:]]*(export[[:space:]]+)?$var=" "$pin")"
    [ "$count" -eq 1 ] || \
        adk_nix_die "$what pin: $count $var lines in $pin, expected 1"
    want="$(sed -n -E "s|^$var=\"(/nix/store/[0-9a-z]{32}-$name)\"\$|\\1|p" "$pin")"
    [ -n "$want" ] || \
        adk_nix_die "$what pin: $var in $pin is not a quoted /nix/store/<hash>-$3 literal"
    # And as a shell reads it, which is how the file's other consumers read it:
    # the whole file has to source cleanly, and then give the literal.
    value="$(adk_nix_pin "$pin" "$var")" || \
        adk_nix_die "$what pin: $pin does not source cleanly"
    [ "$value" = "$want" ] || \
        adk_nix_die "$what pin: $pin sets $var to something other than its literal"

    got="$(nix eval --raw --max-jobs 0 --no-update-lock-file \
               --option allow-import-from-derivation false "$at#$attr")" || \
        adk_nix_die "$what pin: nix eval of $at#$attr failed"
    [ -n "$got" ] || adk_nix_die "$what pin: nix eval of $at#$attr printed nothing"
    [ "$got" = "$want" ] || \
        adk_nix_die "$what pin: staging.pin names $want, the flake's $attr is $got"
    adk_nix_note "$what pin: $want is the flake's $attr"
}

# The store names as the packages write them; build.sh and run.sh take the
# same ones.
check_outpath openroad NIX_OPENROAD_OUTPATH openroad 'openroad[-.+_0-9A-Za-z]*'
check_outpath librelane NIX_LIBRELANE_OUTPATH librelane 'python3[.0-9]*-librelane-[-.+_0-9A-Za-z]*'
check_outpath librelane-python NIX_LIBRELANE_PYTHON_OUTPATH librelane-python 'python3-[.0-9]*-env'
check_outpath sg13g2-pdk NIX_SG13G2_PDK_OUTPATH sg13g2-pdk 'ihp-open-pdk-[0-9a-f]{7}'
check_outpath python NIX_PYTHON_OUTPATH python 'python3-[.0-9]*-env'
check_outpath cosim-iverilog NIX_COSIM_IVERILOG_OUTPATH cosim-iverilog 'iverilog-[-.+_0-9A-Za-z]*'
check_outpath cosim-cocotb NIX_COSIM_COCOTB_OUTPATH cosim-cocotb 'python3[.0-9]*-cocotb-[.0-9]*'
check_outpath cosim-python NIX_COSIM_PYTHON_OUTPATH cosim-python 'python3-[.0-9]*-env'
check_outpath cosim-devshell NIX_COSIM_DEVSHELL_OUTPATH cosim-devshell 'cosim-devshell'
check_outpath mixsim-ngspice NIX_MIXSIM_NGSPICE_OUTPATH mixsim-ngspice 'ngspice-[.0-9]+'
check_outpath mixsim-iverilog NIX_MIXSIM_IVERILOG_OUTPATH mixsim-iverilog 'iverilog-libvvp-[-.+_0-9A-Za-z]*'
check_outpath sg13g2-osdi NIX_SG13G2_OSDI_OUTPATH sg13g2-osdi 'sg13g2-osdi-[0-9a-f]{7}'
check_outpath gtkwave NIX_GTKWAVE_OUTPATH gtkwave 'gtkwave-[.0-9]+'
check_outpath gaw NIX_GAW_OUTPATH gaw 'gaw3-[0-9]{8}'
check_outpath xschem NIX_XSCHEM_OUTPATH xschem 'xschem-[.0-9]+(RC)?-[0-9a-f]{7}'
check_outpath vams-gnucap NIX_VAMS_GNUCAP_OUTPATH vams-gnucap 'gnucap-[0-9a-f]{7}-modelgen-verilog-[0-9a-f]{7}'
check_outpath vams-devshell NIX_VAMS_DEVSHELL_OUTPATH vams-devshell 'vams-devshell'
