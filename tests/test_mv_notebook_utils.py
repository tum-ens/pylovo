"""Focused checks for the read-only MV notebook helpers: variant selection, planning cases and parameter tables."""

import sys
from pathlib import Path

import pandas as pd

from pylovo import config_loader

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "notebook_tutorials" / "mv_grids"))
import mv_notebook_utils as mvu


def _variant(parameters):
    return mvu.Variant(
        version_id="mv2", variant_id="sample", label="sample",
        row={"mv_parameters": parameters}, nodes=None, lines=None,
        grids=pd.DataFrame({"mv_grid_id": [7], "metrics": [{
            "load_converged": True, "n1a_converged": True, "n1b_converged": False,
            "feed_in_wind_converged": True, "feed_in_pv_converged": True,
        }]}),
        substations=None, settlements=None, rings=None, parts=None,
    )


class FakeClient:
    """``list_variants`` of a version with a current, a newer current, a retired and a partial variant."""

    def __init__(self):
        self.conn = self

    def list_variants(self, version):
        assert version == "mv2"
        return pd.DataFrame({
            "mv_variant_id": ["d1", "load8", "load9", "run"],
            "status": ["done", "done", "done", "partial"],
            "retired": [["generation.include_generation"], [], [], []],
            "created_at": pd.to_datetime(["2026-10-21", "2026-10-19", "2026-10-20", "2026-10-22"]),
            "finished_at": pd.to_datetime(["2026-10-21", "2026-10-19", "2026-10-20", None]),
        })

    def rollback(self):
        pass


def test_case_table_has_the_planning_cases_only():
    table = mvu.case_table(_variant({"generation": {"include_generation": True}}))
    assert list(table["case"]) == ["load", "n1a", "n1b"]
    assert list(table["converged"]) == [True, True, False]


def test_default_variant_is_the_configured_one_if_it_exists(monkeypatch):
    monkeypatch.setattr(config_loader, "MV_VARIANT_ID", "load8")
    assert mvu.resolve(FakeClient(), "mv2")[1] == ["load8"]


def test_default_variant_else_the_newest_done_variant_that_is_not_retired(monkeypatch):
    monkeypatch.setattr(config_loader, "MV_VARIANT_ID", "absent")
    assert mvu.resolve(FakeClient(), "mv2")[1] == ["load9"]


def test_current_selection_and_explicit_choice(monkeypatch, capsys):
    monkeypatch.setattr(config_loader, "MV_VARIANT_ID", "absent")
    assert mvu.resolve(FakeClient(), "mv2", select="current")[1] == ["load8", "load9"]
    assert mvu.resolve(FakeClient(), "mv2", variants=["d1", "x"])[1] == ["d1"]
    out = capsys.readouterr().out
    assert "not in version mv2: x" in out
    assert "d1: older variant" in out


def test_variant_parameters_leave_out_removed_options():
    snapshot = {"generation": {"include_generation": False, "lv_pv_cos_phi": 0.95},
                "cases": {"feed_in_pv": {"pv": 1.0}}, "topology": {"solver": "savings", "stub_max_kva": 1000.0},
                "voltage": {"max_rise_percent": 2.0, "busbar_vm_pu": 1.02}}
    assert mvu.variant_parameters(snapshot) == {"topology.stub_max_kva": 1000.0, "voltage.busbar_vm_pu": 1.02}
    assert mvu.dimension_of("topology.stub_max_kva") == "stubs and links"
    assert mvu.dimension_of("topology.local_search_rounds") == "solver"
