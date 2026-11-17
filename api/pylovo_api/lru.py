"""A small thread-safe least-recently-used cache, for the caches of the API and of ``pylovo-viewer``.

Imports nothing of pylovo, so the viewer can use it without loading pylovo's configuration.
(:class:`pylovo.load_editing.NetCache` is the library's own copy: pylovo does not depend on the API.)
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any


class LRU:
    """A small thread-safe least-recently-used cache; ``get`` returns None for a missing key."""

    def __init__(self, size: int) -> None:
        self.size, self._data, self._lock = size, OrderedDict(), threading.Lock()

    def get(self, key: Any) -> Any:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return self._data[key]
        return None

    def put(self, key: Any, value: Any) -> Any:
        """Store ``value`` (the least recently used entry beyond ``size`` is dropped) and return it."""
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.size:
                self._data.popitem(last=False)
        return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
