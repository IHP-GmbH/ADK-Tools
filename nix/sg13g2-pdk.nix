# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# PDK_ROOT for the die builds, with PDK=ihp-sg13g2: the IHP-Open-PDK parent and
# the submodules given copied into one tree with the repository's layout, and
# at its root the statement nix/pdk-source.nix makes of them. Copied, not joined
# with symlinks: chiplet-flow's emitter resolves every declared PDK input and
# refuses one that resolves outside PDK_ROOT, which a symlink into another store
# path does. The parent's own symlinks are relative and stay inside the tree.
# The submodule commits the parent names (its gitlinks) are not in its tarball,
# so comparing them is the pins check's job.
#
# parent and submodules as nix/pdk-source.nix takes them.
{
  lib,
  runCommandLocal,
  parent,
  submodules,
}:
let
  source = import ./pdk-source.nix { inherit parent submodules; };
  inherit (source) statement;
in
# No passthru identity: the build context record reads the statement itself.
runCommandLocal "ihp-open-pdk-${builtins.substring 0 7 statement.parent.rev}" { }
  (
    ''
      cp -r ${parent.src}/. "$out"
      chmod -R u+w "$out"
    ''
    + lib.concatStrings (
      lib.mapAttrsToList (path: s: ''
        if [ -n "$(ls -A "$out/${path}" 2>/dev/null)" ]; then
          echo "${path}: not empty in the parent tree" >&2
          exit 1
        fi
        mkdir -p "$out/${path}"
        cp -r ${s.src}/. "$out/${path}/"
      '') submodules
    )
    # The placeholder in the text below becomes this derivation's $out.
    + ''
      if [ -e "$out/${source.file}" ] || [ -L "$out/${source.file}" ]; then
        echo "${source.file}: already in the parent tree" >&2
        exit 1
      fi
      printf '%s\n' ${lib.escapeShellArg source.json} > "$out/${source.file}"
      chmod a-w "$out/${source.file}"
    ''
  )
