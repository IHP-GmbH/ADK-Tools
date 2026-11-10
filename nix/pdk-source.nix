# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# The statement a materialized sg13g2 PDK tree makes about its own sources:
# pdk-source.json at the tree root, format adk-tools-pdk-source/v1
# (docs/E2E_BUILD_VARIANT.md, "Store-PDK identity"). This is the one place the
# file's name and format are written. Builtins only, so nix/test-pdk-source.sh
# evaluates it on stub inputs, without nixpkgs and without a fetch.
#
#   parent      { input = "<flake input name>"; src = <that input>; }
#   submodules  { "<gitlink path>" = { input; src; }; }, the ones copied in
#
# Every value is the evaluated input's own (src.rev, src.narHash), never read
# from flake.lock. An input without them, a path: override or a dirty tree,
# fails the evaluation.
#
# Returns { file, format, statement, json }. statement is the file's content
# without materialized_at. json is the whole content, materialized_at being
# builtins.placeholder "out": it means something only in the text of the
# derivation that writes it (nix/sg13g2-pdk.nix), where Nix puts that
# derivation's own $out in its place.
{ parent, submodules }:
let
  file = "pdk-source.json";
  format = "adk-tools-pdk-source/v1";

  lines = s: builtins.filter builtins.isString (builtins.split "\n" s);
  count = x: xs: builtins.length (builtins.filter (y: y == x) xs);

  identity =
    what: input:
    if !(builtins.isString (input.input or null)) || input.input == "" then
      throw "pdk-source: ${what} names no flake input"
    else if builtins.match "[0-9a-f]{40}" (input.src.rev or "") == null then
      throw "pdk-source: ${what} (${input.input}) has no 40-hex rev: a path: or dirty input cannot state one"
    else if builtins.match "sha256-[A-Za-z0-9+/]{43}=" (input.src.narHash or "") == null then
      throw "pdk-source: ${what} (${input.input}) has no sha256 narHash"
    else
      {
        inherit (input) input;
        inherit (input.src) rev narHash;
      };

  # The value of a `path = ...` line of .gitmodules, or null for any other
  # line. A line in a form this does not read fails the evaluation rather
  # than leave a submodule out of not_fetched or put another path in it: a
  # value quoted, escaped, carrying a comment or empty, and a section header
  # other than [submodule "<name>"] alone on its line (a key after it, or a
  # path key in another section such as [include]).
  pathValue =
    line:
    let
      header = builtins.match "[[:space:]]*\\[.*" line != null;
      submodule =
        builtins.match "[[:space:]]*\\[[Ss][Uu][Bb][Mm][Oo][Dd][Uu][Ll][Ee][[:space:]]+\"[^\"\\\\]*\"][[:space:]]*" line
        != null;
      m = builtins.match "[[:space:]]*[Pp][Aa][Tt][Hh][[:space:]]*=(.*)" line;
      v = builtins.match "[[:space:]]*([^[:space:]](.*[^[:space:]])?)[[:space:]]*" (builtins.head m);
    in
    if header && !submodule then
      throw "pdk-source: a .gitmodules section line this does not read: ${line}"
    else if m == null then
      null
    else if v == null || builtins.match ".*[\"\\\\#;].*" (builtins.head v) != null then
      throw "pdk-source: a .gitmodules path line this does not read: ${line}"
    else
      builtins.head v;

  declared = builtins.filter (p: p != null) (
    map pathValue (lines (builtins.readFile "${parent.src}/.gitmodules"))
  );
  fetched = builtins.attrNames submodules;
  duplicated = builtins.filter (p: count p declared > 1) declared;
  undeclared = builtins.filter (p: !(builtins.elem p declared)) fetched;

  statement =
    if duplicated != [ ] then
      throw "pdk-source: the parent's .gitmodules declares ${builtins.head duplicated} more than once"
    else if undeclared != [ ] then
      throw "pdk-source: ${builtins.head undeclared} is copied in, but the parent's .gitmodules does not declare it"
    else
      {
        inherit format;
        parent = identity "the parent" parent;
        # attrNames is sorted, so the list is sorted by path.
        submodules = map (path: { inherit path; } // identity path submodules.${path}) fetched;
        not_fetched = builtins.sort builtins.lessThan (
          builtins.filter (p: !(builtins.elem p fetched)) declared
        );
      };
in
{
  inherit file format statement;
  # Sorted keys (toJSON writes an attrset's in order), no whitespace.
  json = builtins.toJSON (statement // { materialized_at = builtins.placeholder "out"; });
}
