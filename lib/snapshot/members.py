"""A member of a JSON object read back from elsewhere, of the type its reader expects.

The plan comes from build meta-data and the failure records from the store,
which other jobs write, so their readers check every member before they use
it. Each reader names the error it raises.
"""

from __future__ import annotations

from typing import Any


def member(
    d: Any, name: str, kind: type | tuple[type, ...], where: str, error: type[Exception]
) -> Any:
    """``d[name]``, which must be of ``kind``; ``error`` naming ``where`` otherwise.

    A bool is no int here, although Python takes it for one.
    """
    if not isinstance(d, dict) or name not in d:
        raise error(f"{where}: no {name}")
    v = d[name]
    if not isinstance(v, kind) or (kind is int and isinstance(v, bool)):
        names = " or ".join(k.__name__ for k in (kind if isinstance(kind, tuple) else (kind,)))
        raise error(f"{where}.{name}: expected {names}, got {type(v).__name__}")
    return v
