#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Refuse an image that is not built on another image's layers.

build.sh checks two relations with it: each image it tags starts with the tools image's
layers (our code on exactly those tools), and the runtime image's layers are the first
layers of the verify image, so the image that ships is the one the verify steps ran on.
The second fails when the runtime build did not reuse all of the verify build's layers and
compiled again: its image was then never verified.
"""

from __future__ import annotations

import json
import sys

USAGE = """Usage: check_layer_prefix.py LOWER LOWER_LAYERS IMAGE IMAGE_LAYERS
    LOWER names the lower image in the messages ("tools", "runtime"); LOWER_LAYERS and
    IMAGE_LAYERS are `docker image inspect -f '{{json .RootFS.Layers}}'` of each. Prints
    "== IMAGE: N LOWER layers, M above them", exits 1 with the reason when IMAGE does not
    start with every lower layer, and 2 on wrong arguments."""


def common(lower: list[str], image: list[str]) -> int:
    """How many layers the two share from the bottom."""
    n = 0
    for a, b in zip(lower, image):
        if a != b:
            break
        n += 1
    return n


def check(lower_name: str, lower: list[str], name: str, image: list[str]) -> str:
    """The line build.sh prints when `image` starts with every layer of `lower`; else
    ValueError with the reason."""
    shared = common(lower, image)
    if shared < len(lower):
        raise ValueError(f"Refusing: {name} is not built on the {lower_name} image's "
                         f"{len(lower)} layers (it shares the first {shared})")
    return f"== {name}: {len(lower)} {lower_name} layers, {len(image) - len(lower)} above them"


def layers(what: str, text: str) -> list[str]:
    """One argument's layer list, or SystemExit 2 saying which argument is not one."""
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        print(f"check_layer_prefix.py: {what} is not a JSON list of layer digests: "
              f"{text[:80]!r}", file=sys.stderr)
        raise SystemExit(2)
    return value


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(USAGE, file=sys.stderr)
        return 2
    lower_name, lower, name, image = argv
    lower_layers = layers(f"the {lower_name} image's layers", lower)
    image_layers = layers(f"{name}'s layers", image)
    try:
        print(check(lower_name, lower_layers, name, image_layers))
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
