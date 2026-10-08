"""Small metadata cache with shared in-flight lookups; never caches media URLs."""
from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
import threading
import time


class ProbeCache:
    def __init__(self, ttl=120, capacity=32):
        self.ttl = ttl
        self.capacity = capacity
        self._lock = threading.Lock()
        self._values = OrderedDict()
        self._pending = {}

    def clear(self):
        with self._lock:
            self._values.clear()
            self._pending.clear()

    def get(self, key, work, refresh=False):
        with self._lock:
            cached = self._values.get(key)
            if cached and not refresh and time.monotonic() - cached[0] < self.ttl:
                self._values.move_to_end(key)
                return deepcopy(cached[1])
            future = self._pending.get(key)
            owner = future is None
            if owner:
                future = self._pending[key] = Future()
        if not owner:
            return deepcopy(future.result())
        try:
            result = work()
            with self._lock:
                if self._pending.get(key) is future:
                    self._values[key] = (time.monotonic(), deepcopy(result))
                    self._values.move_to_end(key)
                    while len(self._values) > self.capacity:
                        self._values.popitem(last=False)
            future.set_result(result)
            return deepcopy(result)
        except Exception as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                if self._pending.get(key) is future:
                    self._pending.pop(key, None)
