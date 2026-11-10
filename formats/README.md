<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 IHP GmbH
-->
# Record formats

The records adk-tools writes for other tools to read. Each format is specified by its JSON Schema
alone; the descriptions in the schema carry what its keywords cannot say (byte form, ordering, what
a null means). This directory is licensed Apache-2.0 (`LICENSE`), so a reader can use the schemas
under a permissive licence.

| Format | Schema | Writer | Reader that parses it |
|--------|--------|--------|-----------------------|
| `adk-tools-host-verify-record/v1` | `adk-tools-host-verify-record.v1.schema.json` | `host-verify.sh` (`record.json` in the record directory) | none today |
| `adk-tools-pdk-source/v1` | `adk-tools-pdk-source.v1.schema.json` | `nix/pdk-source.nix` via `nix/sg13g2-pdk.nix` (`pdk-source.json` at the PDK tree root) | chiplet-flow `src/chiplet_die_flow/pdk_source.py` (`read_pdk_source`) |
| `adk-tools-tool-identity/v1` | `adk-tools-tool-identity.v1.schema.json` | `nix/tool_identity.py` (`/opt/adk-tools/tool-identity.json` in an image, written in the runtime stage and checked by verify step 0d; `host` mode writes one from the host's store) | none today |

Versioning: a change to a key or to its meaning bumps `/vN`; readers may reject keys they do not
know.

Not published yet: the execution-context records `adk-tools-nix-context/v0` and
`adk-tools-nix-build-context/v0`. They are not a contract; they get a schema here when they reach v1.

The schemas use a small subset of JSON Schema that `tests/formats/check_format.py` reads with the
standard library. That checker runs on real writer output in `tests/host_verify/`,
`nix/test-pdk-source.sh` and `nix/test_tool_identity.py`.
