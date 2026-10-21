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
