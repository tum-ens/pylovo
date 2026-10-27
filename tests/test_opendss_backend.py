"""The OpenDSS backend on pylovo's specs, compared with the pandapower backend (needs the opendss extra)."""
from __future__ import annotations

import logging

import pytest

altdss = pytest.importorskip("altdss")

from pylovo.electrical_backend.opendss.backend import OpenDSSBackend  # noqa: E402

LOGGER = logging.getLogger("test")


def test_the_first_circuit_of_a_process_runs_at_50_hz():
    altdss.altdss("Clear")
    altdss.altdss("Set DefaultBaseFrequency=60")        # the OpenDSS default in a new process
    backend = OpenDSSBackend(logger=LOGGER)
    backend.initialize_circuit(name="PLZ1_kcid1_bcid1", source_bus="MVbus_1", primary_kv=20.0)
    assert [source.BaseFreq for source in backend.dss.Vsource] == [50.0]
