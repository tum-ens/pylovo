"""Deprecated plotting functions: they still work, warn on every call and will be removed in pylovo 2.0."""
from __future__ import annotations

import functools
import warnings
from typing import Callable, TypeVar

F = TypeVar("F", bound=Callable)

#: The release that removes the deprecated functions.
REMOVED_IN = "2.0"


def deprecated(replacement: str) -> Callable[[F], F]:
    """Mark a function as deprecated: every call warns with a ``DeprecationWarning`` that names ``replacement``.

    The function itself is unchanged and reachable as ``__wrapped__`` (calls inside pylovo use it, so a deprecated
    function that uses another one warns only once).
    """
    def decorate(function: F) -> F:
        message = (f"{function.__module__}.{function.__qualname__} is deprecated and will be removed in pylovo "
                   f"{REMOVED_IN}; use {replacement} instead")

        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            warnings.warn(message, DeprecationWarning, stacklevel=2)
            return function(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate
