"""The input of one district for the planner, read from a fake database."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import shapely

from pylovo.mv_grid import district_input
from pylovo.mv_grid.model import NODE_COLUMNS, EdgeSet
from pylovo.mv_grid.parameters import LvVersionParameters, MvParameters

LV_SNAPSHOT = {
    "load_calculation": {"sim_factor": {"Residential": 0.07, "Commercial": 0.5, "Public": 0.6, "Unknown": 0.6},
                         "consumer_categories": [{"definition": "Commercial", "peak_load_per_m2": 79.0},
                                                 {"definition": "Public", "peak_load_per_m2": 29.0},
                                                 {"definition": "Unknown", "peak_load_per_m2": 29.0}],
                         "default_power_factor": 0.95},
    "cable_dimensioning": {"mv_direct_connection_load_threshold_kw": 300},
    "power_flow_assessment": {"lv_reference_voltage_pu": 0.96},
}
LV = LvVersionParameters.from_generation_parameters("mv1", LV_SNAPSHOT)


def district_nodes() -> pd.DataFrame:
    rows = []
    for i, (kind, gr, offset) in enumerate([("busbar", np.nan, 150.0), ("station", 7, 0.0), ("station", 8, 2500.0),
                                            ("customer", np.nan, 30.0)]):
        row = {c: 0.0 for c in NODE_COLUMNS}
        row.update(kind=kind, connection_hint="busbar" if kind == "busbar" else "ring", x=100.0 * i, y=0.0,
                   attach_edge_id=10 + i, attach_fraction=0.25, attach_offset_m=offset, grid_result_id=gr,
                   lv_design_source_vm_pu=np.nan)
        rows.append(row)
    return pd.DataFrame(rows, index=pd.Index([1, 2, 3, 4], name="mv_node_id"))[list(NODE_COLUMNS)]


class WorkerDb:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.calls: list[tuple] = []
        self.closed = False

    def load_district_nodes(self, mv_grid_id):
        self.calls.append(("load", mv_grid_id))
        if self.fail_on == "load":
            raise LookupError(f"MV grid {mv_grid_id} does not exist")
        return district_nodes(), {"version_id": "mv1", "mv_variant_id": "d0", "substation_id": 5, "vn_kv": 20.0}

    def lv_design_source_vm(self, version_id, ids):
        self.calls.append(("requirement", list(ids)))
        return {7: 0.98, 8: 0.975}

    def subgraph_edges(self, graph_id, hull_wkb, buffer_m, must):
        self.calls.append(("subgraph", graph_id, buffer_m, list(must)))
        return EdgeSet(*(np.zeros(0) for _ in range(7)), np.zeros((0, 2)))

    def edge_geometries(self, graph_id, ids):
        self.calls.append(("geometries", list(ids)))
        return {i: shapely.LineString([(0, 0), (i, 1)]) for i in ids}

    def rollback_changes(self):
        self.calls.append(("rollback",))

    def save_district(self, version_id, variant_id, mv_grid_id, plan, runtime_s, *, requirements=None):
        self.calls.append(("save", mv_grid_id, plan, requirements))

    def mark_grid_failed(self, mv_grid_id, error):
        self.calls.append(("failed", mv_grid_id, error))

    def close(self):
        self.closed = True


def test_district_input_reads_the_district(monkeypatch):
    db = WorkerDb()
    params = MvParameters(subgraph_buffer_m=2000.0)
    inp, requirements = district_input.district_input(db, "mv1", "d0", 6, "g123", params, LV)
    assert requirements == {7: 0.98, 8: 0.975}
    assert inp.nodes["lv_design_source_vm_pu"].tolist()[1:3] == [0.98, 0.975]
    assert np.isnan(inp.nodes["lv_design_source_vm_pu"].iloc[0])
    (_, graph_id, buffer_m, must), = [c for c in db.calls if c[0] == "subgraph"]
    assert graph_id == "g123" and buffer_m == 2600.0 and must == [10, 11, 12, 13]
    assert inp.substation_id == 5 and inp.vn_kv == 20.0
    assert set(inp.edge_geometry([3, 4])) == {3, 4} and set(inp.edge_geometry([4, 5])) == {4, 5}
    assert set(inp.edge_geometry([10, 13])) == {10, 13}
    assert [c[1] for c in db.calls if c[0] == "geometries"] == [[10, 11, 12, 13], [3, 4], [5]]   # once per edge
    # the street point of each node on its attach edge (LineString (0, 0)-(edge, 1) at attach_fraction 0.25)
    assert inp.nodes["ax"].tolist() == [2.5, 2.75, 3.0, 3.25] and inp.nodes["ay"].tolist() == [0.25] * 4
    with pytest.raises(ValueError, match="belongs to variant d0 of version mv1"):
        district_input.district_input(db, "mv1", "d9", 6, "g123", params, LV)
