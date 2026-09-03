"""The private repository names orbatch was extracted from.

orbatch is public and repo-agnostic, so a guard that spells the names it
forbids would reintroduce them: greppable and indexable, in the one file
nobody would think to scrub. The names are therefore stored base64-encoded,
and published as the SHA-256 digests in `DIGESTS`, so this module scans
clean under its own checker and needs no self-exemption.

The obfuscation does no security work. It only keeps the names out of a
public checkout.

Matching is by substring, not by word: a name is forbidden wherever it
appears, including joined to a neighbour by a hyphen or embedded in a
longer identifier.
"""

from __future__ import annotations

import base64
from hashlib import sha256
from typing import Final

_ENCODED: Final = ("cGlua3k=", "bW9pc3QtY3VwY2FrZQ==")

NAMES: Final = tuple(base64.b64decode(name).decode() for name in _ENCODED)

DIGESTS: Final = frozenset(sha256(name.encode()).hexdigest() for name in NAMES)


def forbidden_words(text: str) -> set[str]:
    lowered = text.lower()
    return {name for name in NAMES if name in lowered}
