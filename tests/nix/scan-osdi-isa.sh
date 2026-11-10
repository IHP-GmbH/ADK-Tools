#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# Scans compiled OSDI models for instructions beyond baseline x86-64, the ones a
# generic build must not hold. The named list, on objdump's AT&T disassembly:
#
#   vex   any mnemonic beginning with "v": the VEX and EVEX encodings of AVX,
#         AVX2, FMA and AVX-512. Excluded are the few legacy and virtualisation
#         mnemonics that also begin with "v" (verr, verw, VMX, SVM).
#   fma   vfmadd*, vfmsub*, vfnmadd*, vfnmsub*, vfmaddsub*, vfmsubadd* (FMA3, FMA4).
#   ymm   any operand in a 256-bit register (%ymm*): AVX and AVX2.
#   zmm   any operand in a 512-bit register (%zmm*) or a mask register (%k0-7):
#         AVX-512.
#
# Usage: scan-osdi-isa.sh OBJDUMP FILE...
# Prints one line per file with the four counts and the instruction total, and
# exits 1 if any file has a nonzero count, 2 on a usage or read error.
set -euo pipefail
[ $# -ge 2 ] || { echo "usage: $0 OBJDUMP FILE..." >&2; exit 2; }
objdump=$1
shift
bad=0
for f in "$@"; do
    [ -r "$f" ] || { echo "$f: not readable" >&2; exit 2; }
    counts=$("$objdump" -d --no-show-raw-insn "$f" | awk -F'\t' '
        $1 ~ /^ *[0-9a-f]+:$/ && NF >= 2 {
            n++
            split($2, w, /[ ]+/)
            m = w[1]
            ops = substr($2, length(m) + 1)
            if (m ~ /^v/ && m !~ /^(verr|verw|vmcall|vmlaunch|vmresume|vmxoff|vmxon|vmread|vmwrite|vmptrld|vmptrst|vmclear|vmfunc|vmrun|vmload|vmsave|vmmcall)$/) vex++
            if (m ~ /^vf(n)?m(add|sub|addsub|subadd)/) fma++
            if (ops ~ /%ymm[0-9]/) ymm++
            if (ops ~ /%zmm[0-9]|%k[0-7]([^0-9]|$)/) zmm++
        }
        END { printf "%d %d %d %d %d\n", vex + 0, fma + 0, ymm + 0, zmm + 0, n + 0 }')
    read -r vex fma ymm zmm total <<< "$counts"
    [ "$total" -gt 0 ] || { echo "$f: no instructions disassembled" >&2; exit 2; }
    printf '%s vex=%d fma=%d ymm=%d zmm=%d instructions=%d\n' \
        "$(basename "$f")" "$vex" "$fma" "$ymm" "$zmm" "$total"
    [ $((vex + fma + ymm + zmm)) -eq 0 ] || bad=1
done
exit "$bad"
