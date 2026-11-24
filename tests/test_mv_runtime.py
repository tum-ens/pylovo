"""The shared helpers of the MV stage: the number of district workers."""
from __future__ import annotations

from pylovo.mv_grid import runtime


def test_worker_count_caps_by_jobs_pending_and_memory(monkeypatch):
    monkeypatch.setattr(runtime, "mem_available_gb", lambda: 10.0)
    assert runtime.worker_count(4, 10) == 4
    assert runtime.worker_count(4, 3) == 3
    assert runtime.worker_count(4, 0) == 0
    assert runtime.worker_count(8, 20, per_worker_gb=4.0) == 2            # (10 - 2) / 4
    monkeypatch.setattr(runtime, "mem_available_gb", lambda: 2.5)
    assert runtime.worker_count(4, 10) == 1
    monkeypatch.setattr(runtime, "mem_available_gb", lambda: None)
    assert runtime.worker_count(None, 10) == min(4, runtime.config().N_JOBS)
    assert runtime.mem_available_gb.__name__ == "<lambda>"
