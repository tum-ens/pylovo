"""The backend registry: pandapower ships, other backends are registered by name (no database)."""
import pytest

import pylovo.electrical_backend as electrical_backend
from pylovo.electrical_backend import available_backends, create_backend, factory, register_backend


def test_pandapower_and_the_experimental_opendss_backend_ship():
    assert tuple(available_backends()) == ("opendss", "pandapower")
    assert "OpenDSSBackend" in electrical_backend.__all__


def test_the_opendss_backend_warns_that_it_is_experimental(monkeypatch, caplog):
    from pylovo.electrical_backend.opendss import backend as opendss

    monkeypatch.setattr(opendss, "altdss", object())          # as if altdss were installed
    with caplog.at_level("WARNING"):
        create_backend("opendss")
    assert opendss.EXPERIMENTAL_WARNING in caplog.text


def test_the_opendss_backend_without_altdss_names_the_missing_package(monkeypatch):
    from pylovo.electrical_backend.opendss import backend as opendss

    monkeypatch.setattr(opendss, "altdss", None)
    with pytest.raises(opendss.OpenDSSBackendError, match="uv sync --extra opendss"):
        create_backend("opendss")


def test_a_registered_backend_is_created_by_name(monkeypatch):
    monkeypatch.setattr(factory, "_REGISTRY", dict(factory._REGISTRY))
    loggers = []
    register_backend("Mine ", lambda logger: loggers.append(logger) or "my backend")
    assert create_backend("mine", logger="log") == "my backend" and loggers == ["log"]
    assert tuple(available_backends()) == ("mine", "opendss", "pandapower")


def test_the_backend_interface_asks_for_no_method_that_nothing_calls():
    from pylovo.analysis.parameter_calculation import ParameterCalculator
    from pylovo.electrical_backend.core.backend_base import IElectricalBackend
    from pylovo.electrical_backend.pandapower.backend import PandapowerBackend

    unused = {"cleanup", "set_bus_coordinates", "set_bus_zone"}
    assert not unused & IElectricalBackend.__abstractmethods__
    assert not any(hasattr(PandapowerBackend, name) for name in unused)
    assert not hasattr(ParameterCalculator, "calculate_household_path_impedance_proxy")
