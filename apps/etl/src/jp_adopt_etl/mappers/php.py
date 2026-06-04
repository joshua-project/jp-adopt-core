"""Shared helper for decoding WordPress ``maybe_serialize`` postmeta values.

WordPress wraps arrays/objects with PHP serialization but leaves scalars
alone. The serialized shape always starts with ``a:`` / ``s:`` / ``i:`` /
``O:`` / ``b:`` so we can detect cheaply before calling phpserialize.
"""

from __future__ import annotations

import logging
from typing import Any

import phpserialize

logger = logging.getLogger(__name__)


def loads_php_maybe(value: Any) -> Any:
    """Deserialize ``value`` if it is a php-serialized string; otherwise
    return it unchanged. On a decode error, log and return the original."""
    if not isinstance(value, str):
        return value
    if len(value) < 2 or value[1] != ":":
        return value
    try:
        return phpserialize.loads(value.encode("utf-8"), decode_strings=True)
    except (ValueError, TypeError, EOFError) as e:
        logger.warning("phpserialize.loads failed for %r: %s", value[:64], e)
        return value


__all__ = ["loads_php_maybe"]
