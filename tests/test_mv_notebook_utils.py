"""Focused checks for the read-only MV notebook's planning-scope interpretation."""

import sys
from pathlib import Path

import pandas as pd

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


def test_case_table_only_includes_requested_load_only_cases():
    variant = _variant({"generation": {"include_generation": False}})
    table = mvu.case_table(variant)
    assert list(table["case"]) == ["load", "n1a", "n1b"]
    assert list(table["converged"]) == [True, True, False]


def test_missing_historical_scope_keeps_five_cases():
    variant = _variant({})
    assert variant.include_generation is True
    assert set(mvu.case_table(variant)["case"]) == {
        "load", "n1a", "n1b", "feed_in_wind", "feed_in_pv",
    }


def test_preferred_variant_falls_back_without_replacing_explicit_choice():
    class FakeClient:
        def __init__(self):
            self.conn = self

        def list_variants(self, version):
            assert version == "mv2"
            return pd.DataFrame({"mv_variant_id": ["d0", "d1"]})

        def rollback(self):
            pass

    dbc = FakeClient()
    assert mvu.resolve(dbc, "mv2", default_variant=("load1", "d1"))[1] == ["d1"]
    assert mvu.resolve(dbc, "mv2", variants=["d0"], default_variant=("load1", "d1"))[1] == ["d0"]
