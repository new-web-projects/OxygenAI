"""
Strict JSON helpers for tests.

Python's `json.loads` quietly accepts the non-standard tokens NaN and
Infinity, which are not valid JSON and which browsers' `JSON.parse` and
most other clients reject. These helpers fail on them, so a test asserting
"the response is valid, finite JSON" means exactly that.
"""

from __future__ import annotations

import json
import math
from typing import Any


def _reject_constant(name: str) -> Any:
    raise ValueError(f"response contains the non-JSON constant {name!r}")


def loads_strict(text: str) -> Any:
    """`json.loads` that raises on NaN / Infinity / -Infinity."""
    return json.loads(text, parse_constant=_reject_constant)


def assert_all_finite(value: Any, path: str = "$") -> None:
    """Walk a decoded JSON value; every number in it must be finite."""
    if isinstance(value, float):
        assert math.isfinite(value), f"non-finite number at {path}: {value!r}"
    elif isinstance(value, dict):
        for key, item in value.items():
            assert_all_finite(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            assert_all_finite(item, f"{path}[{index}]")
