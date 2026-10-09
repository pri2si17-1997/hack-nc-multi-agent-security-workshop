"""Content fingerprints: what a physician signs is exactly what gets acted on.

A signature is bound to the SHA-256 of the signed content (canonical JSON). Anyone downstream
(the approval service, the receiving pharmacy) can recompute it and refuse anything that differs.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def fingerprint(content: Any) -> str:
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
