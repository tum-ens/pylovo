"""The backend registry: pandapower ships, other backends are registered by name (no database)."""
import pytest

import pylovo.electrical_backend as electrical_backend
from pylovo.electrical_backend import available_backends, create_backend, factory, register_backend


def test_only_the_pandapower_backend_ships():
    assert tuple(available_backends()) == ("pandapower",)
    assert "OpenDSSBackend" not in electrical_backend.__all__ and not hasattr(electrical_backend, "OpenDSSBackend")


def test_the_removed_opendss_name_is_refused_with_the_available_names():
    with pytest.raises(ValueError, match="Unknown backend: opendss. Available: pandapower"):
        create_backend("opendss")


def test_a_registered_backend_is_created_by_name(monkeypatch):
    monkeypatch.setattr(factory, "_REGISTRY", dict(factory._REGISTRY))
    loggers = []
    register_backend("Mine ", lambda logger: loggers.append(logger) or "my backend")
    assert create_backend("mine", logger="log") == "my backend" and loggers == ["log"]
    assert tuple(available_backends()) == ("mine", "pandapower")


def test_the_backend_interface_asks_for_no_method_that_nothing_calls():
    from pylovo.analysis.parameter_calculation import ParameterCalculator
    from pylovo.electrical_backend.core.backend_base import IElectricalBackend
    from pylovo.electrical_backend.pandapower.backend import PandapowerBackend

    unused = {"cleanup", "set_bus_coordinates", "set_bus_zone"}
    assert not unused & IElectricalBackend.__abstractmethods__
    assert not any(hasattr(PandapowerBackend, name) for name in unused)
    assert not hasattr(ParameterCalculator, "calculate_household_path_impedance_proxy")
