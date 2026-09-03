"""The private repository names orbatch was extracted from, as digests.

orbatch is public and repo-agnostic, so a guard that spells the names it
forbids would reintroduce them: greppable and indexable, in the one file
nobody would think to scrub. The names are therefore stored base64-encoded
and matched by SHA-256 digest, and this module scans clean under its own
checker.

The obfuscation does no security work. It only keeps the names out of a
public checkout.
"""

from __future__ import annotations

import base64
import re
from hashlib import sha256
from typing import Final

_ENCODED: Final = ("cGlua3k=", "bW9pc3QtY3VwY2FrZQ==")

NAMES: Final = tuple(base64.b64decode(name).decode() for name in _ENCODED)

DIGESTS: Final = frozenset(sha256(name.encode()).hexdigest() for name in NAMES)

# The hyphen is inside the class so a two-part name survives as one token.
_SEPARATOR: Final = re.compile(r"[^a-z0-9-]+")


def forbidden_words(text: str) -> set[str]:
    return {
        word
        for word in set(_SEPARATOR.split(text.lower()))
        if sha256(word.encode()).hexdigest() in DIGESTS
    }
