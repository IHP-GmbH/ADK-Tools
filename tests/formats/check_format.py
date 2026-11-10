"""Check a record against its schema in formats/, with the standard library only.

The schemas in formats/ are the specification of adk-tools' published record formats. This checker
reads a deliberately small subset of JSON Schema, so that it needs no jsonschema dependency and so
that a schema cannot say something this checker would skip:

- the root may carry "$schema", "$comment", "title" and "description" besides its body;
- anywhere else only type, const, enum, pattern, properties, required, additionalProperties, items
  and description are allowed, and any other keyword fails;
- every object schema has additionalProperties false and requires every property it names, so the
  key set of each object in a record must be exactly the schema's, at every level.

The writers emit every key (an unknown value is null, never an absent key), which is why required
equals properties. A schema that breaks one of these rules fails before any record is read.

A pattern is searched, as in JSON Schema, and its trailing $ means the end of the string, as in
ECMA-262. Python's $ also matches before a final newline, so it is read as \\Z.

Run: python3 tests/formats/check_format.py <schema.json> <record.json>
Exit 0 when the record conforms, 1 with one line per finding when it does not, 2 on a bad schema.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT_ONLY = {"$schema", "$comment", "title"}
KEYWORDS = {"type", "const", "enum", "pattern", "properties", "required", "additionalProperties",
            "items", "description"}
TYPES = {"null", "boolean", "integer", "number", "string", "array", "object"}


def _python_pattern(pattern: str) -> str:
    """pattern with a trailing unescaped $ read as the end of the string (\\Z), as ECMA-262 reads it."""
    if not pattern.endswith("$"):
        return pattern
    body = pattern[:-1]
    backslashes = len(body) - len(body.rstrip("\\"))
    return body + r"\Z" if backslashes % 2 == 0 else pattern


class SchemaError(Exception):
    """The schema uses something this checker does not read, or breaks the exact-keys rule."""


def lint(schema: dict, where: str = "#", root: bool = True) -> None:
    """Raise SchemaError on the first rule the schema breaks."""
    if not isinstance(schema, dict):
        raise SchemaError(f"{where}: a schema must be an object")
    allowed = KEYWORDS | (ROOT_ONLY if root else set())
    unknown = sorted(set(schema) - allowed)
    if unknown:
        raise SchemaError(f"{where}: keyword(s) this checker does not read: {', '.join(unknown)}")
    if "description" in schema and not isinstance(schema["description"], str):
        raise SchemaError(f"{where}: description must be a string")
    types = schema.get("type")
    if types is not None:
        names = types if isinstance(types, list) else [types]
        if not names or any(t not in TYPES for t in names) or len(set(names)) != len(names):
            raise SchemaError(f"{where}: type must name distinct JSON types, got {types!r}")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise SchemaError(f"{where}: enum must be a non-empty list")
    if "pattern" in schema:
        if not isinstance(schema["pattern"], str):
            raise SchemaError(f"{where}: pattern must be a string")
        try:
            re.compile(_python_pattern(schema["pattern"]))
        except re.error as err:
            raise SchemaError(f"{where}: pattern does not compile: {err}") from None
    names = types if isinstance(types, list) else [types]
    if "properties" in schema or "object" in names:
        props = schema.get("properties")
        if not isinstance(props, dict):
            raise SchemaError(f"{where}: an object schema must name its properties")
        if schema.get("additionalProperties") is not False:
            raise SchemaError(f"{where}: an object schema must set additionalProperties to false")
        if sorted(schema.get("required", [])) != sorted(props):
            raise SchemaError(f"{where}: required must list every property, exactly once")
        for key, sub in props.items():
            lint(sub, f"{where}/properties/{key}", root=False)
    elif "additionalProperties" in schema or "required" in schema:
        raise SchemaError(f"{where}: additionalProperties or required outside an object schema")
    if "items" in schema:
        lint(schema["items"], f"{where}/items", root=False)
    elif "array" in names:
        raise SchemaError(f"{where}: an array schema must say what its items are")


def _type_of(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def check(schema: dict, value, where: str = "") -> list[str]:
    """Findings for value against a schema that passed lint; empty when it conforms."""
    here = where or "(root)"
    found = []
    types = schema.get("type")
    if types is not None:
        names = types if isinstance(types, list) else [types]
        got = _type_of(value)
        if got not in names and not (got == "integer" and "number" in names):
            return [f"{here}: type {got}, the schema wants {' or '.join(names)}"]
    if "const" in schema and value != schema["const"]:
        found.append(f"{here}: {value!r}, the schema wants {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        found.append(f"{here}: {value!r} is not one of {schema['enum']!r}")
    if "pattern" in schema and isinstance(value, str) and not re.search(_python_pattern(schema["pattern"]), value):
        found.append(f"{here}: {value!r} does not match {schema['pattern']!r}")
    if isinstance(value, dict) and "properties" in schema:
        props = schema["properties"]
        extra = sorted(set(value) - set(props))
        missing = sorted(set(props) - set(value))
        if extra:
            found.append(f"{here}: key(s) the schema does not name: {', '.join(extra)}")
        if missing:
            found.append(f"{here}: key(s) the schema requires but the record lacks: {', '.join(missing)}")
        for key in sorted(set(value) & set(props)):
            found += check(props[key], value[key], f"{where}/{key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            found += check(schema["items"], item, f"{where}/{i}")
    return found


def check_file(schema_path: Path, record_path: Path) -> list[str]:
    schema = json.loads(Path(schema_path).read_text())
    lint(schema)
    return check(schema, json.loads(Path(record_path).read_text()))


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[-2], file=sys.stderr)
        return 2
    try:
        found = check_file(Path(argv[1]), Path(argv[2]))
    except SchemaError as err:
        print(f"schema {argv[1]}: {err}", file=sys.stderr)
        return 2
    for line in found:
        print(f"{argv[2]}: {line}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
