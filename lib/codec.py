"""Dataclasses as canonical JSON, and back.

Every document the plugin stores (a manifest, a plan, a failure record) is a frozen dataclass
whose fields are the document's members, so neither direction is written by hand.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, TypeVar

import dacite

T = TypeVar("T")

# JSON has lists only: a field typed as a tuple takes one.
READ = dacite.Config(cast=[tuple])


def canonical_json(v: Any) -> bytes:
    """``v`` as JSON with members sorted, no white space, non-ASCII as itself, in UTF-8."""
    text = json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")


def encoded(x: Any) -> bytes:
    """The dataclass ``x`` as canonical JSON."""
    return canonical_json(dataclasses.asdict(x))


def decoded(cls: type[T], body: str | bytes) -> T:
    """The ``cls`` that ``encoded`` wrote into ``body``."""
    return dacite.from_dict(cls, json.loads(body), READ)
