"""MV stage parameters without a database: options, MvParameters, its snapshot, LvVersionParameters, contracts.

The line and transformer types normally come from ``pylovo.mv_grid.equipment`` (pandapower); most tests use a
small fake of that module (line values of pandapower 3.4.0, made-up transformer values), so they do not depend
on it.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import logging
import pickle
import re
import sys
import types

import numpy as np
import pytest

from pylovo import version_snapshot
from pylovo.mv_grid import options
from pylovo.mv_grid.model import CATEGORIES, HvMvType, LineType, MvRunSummary
from pylovo.mv_grid.options import (
    CONSTANTS,
    MV_STAGE_VERSION,
    YAML_KEYS,
    check_mv_combination,
    coerce_mv_value,
    parse_assignment,
)
from pylovo.mv_grid.parameters import LvVersionParameters, MvParameters, MvStageError
from pylovo.version_snapshot import ADDED_LATER, MV_ADDED_LATER, UNKNOWN, compare_snapshots

LINE_TYPES = {
    "NA2XS2Y 1x240 RM/25 12/20 kV": LineType("NA2XS2Y 1x240 RM/25 12/20 kV", "cable", 20.0, 0.122, 0.112, 304.0, 0.421),
    "NA2XS2Y 1x150 RM/25 12/20 kV": LineType("NA2XS2Y 1x150 RM/25 12/20 kV", "cable", 20.0, 0.206, 0.116, 250.0, 0.319),
    "NA2XS2Y 1x240 RM/25 6/10 kV": LineType("NA2XS2Y 1x240 RM/25 6/10 kV", "cable", 10.0, 0.122, 0.105, 456.0, 0.416),
    "122-AL1/20-ST1A 20.0": LineType("122-AL1/20-ST1A 20.0", "overhead", 20.0, 0.2376, 0.344, 10.3, 0.41),
    "122-AL1/20-ST1A 10.0": LineType("122-AL1/20-ST1A 10.0", "overhead", 10.0, 0.2376, 0.323, 11.1, 0.41),
    "70-AL1/11-ST1A 20.0": LineType("70-AL1/11-ST1A 20.0", "overhead", 20.0, 0.4132, 0.36, 9.7, 0.29),
    "NA2XS2Y 1x150 RM/25 6/10 kV": LineType("NA2XS2Y 1x150 RM/25 6/10 kV", "cable", 10.0, 0.206, 0.11, 360.0, 0.315),
    "48-AL1/8-ST1A 20.0": LineType("48-AL1/8-ST1A 20.0", "overhead", 20.0, 0.5939, 0.372, 9.5, 0.21),
    "48-AL1/8-ST1A 10.0": LineType("48-AL1/8-ST1A 10.0", "overhead", 10.0, 0.5939, 0.35, 10.1, 0.21),
}
#: The starting conductors of the MV planner (equipment.DEFAULT_LINE_TYPES).
DEFAULT_TYPES = {20.0: ("NA2XS2Y 1x240 RM/25 12/20 kV", "122-AL1/20-ST1A 20.0"),
                 10.0: ("NA2XS2Y 1x240 RM/25 6/10 kV", "122-AL1/20-ST1A 10.0")}


def fake_hv_mv_type(sn_mva: float, vn_lv_kv: float) -> HvMvType:
    return HvMvType(f"{sn_mva:g} MVA 110/{vn_lv_kv:g} kV", sn_mva, 110.0, vn_lv_kv, 12.0, 0.4, 20.0, 0.06, 150.0,
                    "YNd5", "hv", 0, -9, 9, 1.5, sn_mva == 31.5)


@pytest.fixture()
def equipment(monkeypatch):
    """A fake ``pylovo.mv_grid.equipment`` with the WP3 interface; ``calls`` counts the resolutions."""
    module = types.ModuleType("pylovo.mv_grid.equipment")
    module.calls = []

    def line_type(name):
        module.calls.append(name)
        return LINE_TYPES[name]

    def check_line_type(lt, vn_kv, kind):
        if lt.vn_kv != vn_kv or lt.kind != kind:
            raise ValueError(f"{lt.name} is no {kind} for {vn_kv:g} kV")

    def hv_mv_types(vn_lv_kv, sizes_mva):
        module.calls.append(tuple(sizes_mva))
        return tuple(fake_hv_mv_type(s, vn_lv_kv) for s in sizes_mva)

    def line_type_name_at(name, vn_kv):
        if name in LINE_TYPES and LINE_TYPES[name].vn_kv == float(vn_kv):
            return name
        pairs = (("12/20 kV", "6/10 kV"), (" 20.0", " 10.0"))
        other = name
        for twenty, ten in pairs:
            other = other.replace(twenty, ten) if float(vn_kv) == 10.0 else other.replace(ten, twenty)
        if other not in LINE_TYPES:
            raise ValueError(f"pandapower has no {vn_kv:g} kV type of the conductor of {name!r}")
        return other

    module.line_type = line_type
    module.line_type_name_at = line_type_name_at
    module.check_line_type = check_line_type
    module.default_line_types = lambda vn_kv: DEFAULT_TYPES[float(vn_kv)]
    module.hv_mv_types = hv_mv_types
    import pylovo.mv_grid as package

    monkeypatch.setitem(sys.modules, "pylovo.mv_grid.equipment", module)
    monkeypatch.setattr(package, "equipment", module, raising=False)
    return module


def record(item) -> dict:
    return dataclasses.asdict(item)


#: The snapshot of the default parameters (DESIGN §6.5) with the fake types: auto voltage, so the equipment holds the
#: 20 kV types and the 10 kV types of the dense districts.
EXPECTED_SNAPSHOT = {
    "schema": 1, "mv_stage_version": 5,
    "voltage": {"vn_kv": "auto", "busbar_vm_pu": 1.02, "busbar_band_pu": 0.015, "max_drop_percent": 5.0,
                "max_drop_n1_percent": 10.0, "max_rise_percent": 2.0, "mv_10kv_min_density_mva_km2": 8.0},
    "loading": {"line_utilization": 0.6, "n1_utilization": 1.0, "hv_mv_n1_utilization": 1.0},
    "sources": {"substations": "osm", "supply_areas": "routed", "region_buffer_m": 5000.0, "substation_merge_m": 75.0,
                "substation_drop_m": 150.0, "ehv_partner_m": 1000.0, "substation_max_attach_m": 2000.0,
                "egon_match_m": 300.0, "rebalance_step_m": 500.0, "rebalance_max_iter": 50},
    "routing": {"mode": "street", "crowfly_detour": 1.3, "overhead_detour": 1.1, "bridge_detour": 1.3,
                "long_connector_m": 300.0, "busbar_exit_radius_m": 200.0, "graph_excluded_klasse": ["Bundesautobahn"],
                "graph_snap_tolerance_m": 0.25,
                "subgraph_buffer_m": 2000.0},
    "settlements": {"buffer_m": 50.0, "spacing_m": 100.0, "min_buildings": 2, "urban_min_buildings": 1000,
                    "polygon_buffer_m": 75.0},
    "topology": {"solver": "savings", "max_stations_per_ring": 20, "stubs": "rural", "overhead_lines": "rural",
                 "interconnection": "ties",
                 "max_half_ring_length_m": 28000.0, "rural_ring_factor": 2.0, "stub_max_kva": 1000.0,
                 "stub_max_length_m": 2000.0, "stub_min_offset_m": 100.0, "loop_in_max_offset_m": 100.0,
                 "generator_max_tee_m": 5000.0, "tie_max_length_m": 2000.0, "local_search_rounds": 50,
                 "neighbours": 15, "ls_time_guard_s": 120.0, "repair_rounds": 4,
                 "busbar_legs": "cable_along_streets", "rural_satellites": "stubs_after_rings"},
    "equipment": {"cable": record(LINE_TYPES["NA2XS2Y 1x240 RM/25 12/20 kV"]),
                  "overhead": record(LINE_TYPES["122-AL1/20-ST1A 20.0"]),
                  "max_parallel_cables": 2, "hv_mv_units": 2,
                  "hv_mv_types": [record(fake_hv_mv_type(s, 20.0)) for s in (25.0, 31.5, 40.0, 63.0)],
                  "cable_10kv": record(LINE_TYPES["NA2XS2Y 1x240 RM/25 6/10 kV"]),
                  "overhead_10kv": record(LINE_TYPES["122-AL1/20-ST1A 10.0"]),
                  "hv_mv_types_10kv": [record(fake_hv_mv_type(s, 10.0)) for s in (25.0, 31.5, 40.0, 63.0)]},
    "loads": {"customer_peak": "sim_factor_times_installed", "dedicated_feeder_kw": 5500.0, "hv_customer_kw": 20000.0,
              "customer_hall_min_footprint_m2": 2000.0, "customer_min_mv_kw": 200.0,
              "busbar_customer_max_m": 1000.0},
    "generation": {"include_generation": False, "lv_side_kw": 200.0, "fallback_kw": {"lv": 100.0, "mvlv": 200.0, "mv": 5500.0, "hvmv": 20000.0},
                   "mv_gen_cos_phi": 0.95, "lv_pv_cos_phi": 1.0, "include_storage": False},
    "cases": {"feed_in_wind": {"load_p": 0.1, "load_q": 0.123, "wind": 1.0, "solar": 0.8, "other": 1.0},
              "feed_in_pv": {"load_p": 0.1, "load_q": 0.123, "wind": 0.85, "solar": 0.95, "other": 1.0}},
    "taps": {"rule": "load_only", "step_percent": 2.5, "min": -2, "max": 2, "lv_design_tolerance_pu": 0.002,
             "lv_feed_in_max_pu": 1.07},
    "coupling": {"merged_scaling": "feeder_category", "lv_pv_at": "LVbus 1"},
}


def json_round_trip(value):
    return json.loads(json.dumps(value, allow_nan=False, sort_keys=True))


# ---------------------------------------------------------------------------------------------- options


def test_specs_have_known_kinds_and_canonical_defaults():
    assert len(YAML_KEYS) == 17 and len(CONSTANTS) >= 50
    assert not set(YAML_KEYS) & set(CONSTANTS)
    for spec in (*YAML_KEYS.values(), *CONSTANTS.values()):
        assert spec.kind in options.KINDS, spec.name
        assert coerce_mv_value(spec.name, copy.deepcopy(spec.default)) == spec.default, spec.name
        assert spec.source, spec.name
    assert all(name.startswith("MV_") for name in YAML_KEYS)
    assert "MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW" not in YAML_KEYS          # an LV-version key
    assert all(spec.field == name for name, spec in CONSTANTS.items())
    assert YAML_KEYS["MV_GRID_GENERATION"].field == "" and YAML_KEYS["MV_GRID_GENERATION"].path == ()


def test_snapshot_paths_are_unique():
    paths = [spec.path for spec in (*YAML_KEYS.values(), *CONSTANTS.values()) if spec.path]
    assert len(paths) == len(set(paths))
    assert {spec.name for spec in YAML_KEYS.values() if not spec.path} == {
        "MV_GRID_GENERATION", "MV_VARIANT_ID", "MV_VARIANT_COMMENT"}


@pytest.mark.parametrize("name, value, expected", [
    ("MV_GRID_GENERATION", True, True),
    ("MV_VARIANT_ID", 7, "7"),
    ("MV_VARIANT_ID", "d1.b-2_x", "d1.b-2_x"),
    ("MV_NOMINAL_VOLTAGE_KV", 10, 10.0),
    ("MV_NOMINAL_VOLTAGE_KV", "auto", "auto"),
    ("MV_NOMINAL_VOLTAGE_KV", " Auto", "auto"),
    ("mv_10kv_min_density_mva_km2", 0.1, 0.1),
    ("MV_MAX_STATIONS_PER_RING", 15.0, 15),
    ("MV_STANDARD_CABLE", None, None),
    ("MV_STANDARD_CABLE", "NA2XS2Y 1x150 RM/25 12/20 kV", "NA2XS2Y 1x150 RM/25 12/20 kV"),
    ("MV_MAX_VOLTAGE_DROP_PERCENT", 4, 4.0),
    ("local_search_rounds", 0, 0),
    ("hv_mv_sizes_mva", [63, 25], (25.0, 63.0)),
    ("graph_excluded_klasse", "Bundesstraße", ("Bundesstraße",)),
    ("graph_excluded_klasse", [], ()),
    ("generator_fallback_kw", {"mv": 5000, "lv": 50, "hvmv": 1e4, "mvlv": 150},
     {"lv": 50.0, "mvlv": 150.0, "mv": 5000.0, "hvmv": 10000.0}),
    ("mvlv_tap_min", -3, -3),
])
def test_coerce_accepts_and_canonicalises(name, value, expected):
    result = coerce_mv_value(name, value)
    assert result == expected and type(result) is type(expected)


@pytest.mark.parametrize("name, value, message", [
    ("MV_GRID_GENERATION", "False", "True or False"),
    ("MV_VARIANT_ID", "x" * 11, "1 to 10 characters"),
    ("MV_VARIANT_ID", "", "1 to 10 characters"),
    ("MV_VARIANT_ID", "a b", "letters, digits"),
    ("MV_VARIANT_ID", 1.5, "a text"),
    ("MV_NOMINAL_VOLTAGE_KV", 15, "one of auto, 20, 10"),
    ("MV_NOMINAL_VOLTAGE_KV", "20 kV", "one of auto, 20, 10"),
    ("MV_NOMINAL_VOLTAGE_KV", True, "one of auto, 20, 10"),
    ("mv_10kv_min_density_mva_km2", -1, "at least 0"),
    ("MV_SUBSTATIONS", "OSM", "one of osm, egon"),
    ("MV_STUBS", False, "one of rural, none"),           # YAML 'no' is False
    ("MV_MAX_STATIONS_PER_RING", 0, "between 1 and 100"),
    ("MV_MAX_STATIONS_PER_RING", 2.5, "an integer"),
    ("MV_MAX_STATIONS_PER_RING", True, "an integer"),
    ("MV_STANDARD_CABLE", "  ", "a name or null"),
    ("MV_LINE_PLANNING_UTILIZATION", 1.2, "between 0.1 and 1"),
    ("MV_LINE_PLANNING_UTILIZATION", float("nan"), "a number"),
    ("MV_LINE_PLANNING_UTILIZATION", "0.6", "a number"),
    ("stub_max_kva", -1, "at least 0"),
    ("hv_mv_sizes_mva", [], "non-empty list"),
    ("hv_mv_sizes_mva", [25, 25.0], "different numbers"),
    ("hv_mv_sizes_mva", 40, "non-empty list"),
    ("graph_excluded_klasse", [1], "a list of names"),
    ("generator_fallback_kw", {"lv": 100}, "the keys lv, mvlv, mv, hvmv"),
    ("feed_in_cases", {"feed_in_wind": {}, "feed_in_pv": {}}, "the keys load_p"),
    ("mvlv_tap_max", -1, "between 0 and 10"),
])
def test_coerce_rejects_with_the_key_name(name, value, message):
    with pytest.raises(ValueError, match=f"^{name} must be") as info:
        coerce_mv_value(name, value)
    assert message in str(info.value)


def test_feed_in_case_factors_are_bounded():
    cases = copy.deepcopy(CONSTANTS["feed_in_cases"].default)
    cases["feed_in_pv"]["solar"] = 1.5
    with pytest.raises(ValueError, match="feed_in_cases must be between 0 and 1"):
        coerce_mv_value("feed_in_cases", cases)


def test_unknown_names_are_rejected_with_a_hint():
    with pytest.raises(ValueError, match="unknown MV parameter 'MV_STUB'.*MV_STUBS"):
        coerce_mv_value("MV_STUB", "none")


def test_parse_assignment():
    assert parse_assignment("MV_STUBS=none") == ("MV_STUBS", "none")
    assert parse_assignment("MV_STANDARD_CABLE=NA2XS2Y 1x150 RM/25 12/20 kV") == (
        "MV_STANDARD_CABLE", "NA2XS2Y 1x150 RM/25 12/20 kV")
    assert parse_assignment("MV_OVERHEAD_LINE=70-AL1/11-ST1A 20.0") == ("MV_OVERHEAD_LINE", "70-AL1/11-ST1A 20.0")
    assert parse_assignment("MV_STANDARD_CABLE=null") == ("MV_STANDARD_CABLE", None)
    assert parse_assignment(" local_search_rounds = 0") == ("local_search_rounds", 0)
    assert parse_assignment("hv_mv_sizes_mva=[40, 25]") == ("hv_mv_sizes_mva", (25.0, 40.0))
    assert parse_assignment("MV_NOMINAL_VOLTAGE_KV=10") == ("MV_NOMINAL_VOLTAGE_KV", 10.0)
    assert parse_assignment("MV_NOMINAL_VOLTAGE_KV=auto") == ("MV_NOMINAL_VOLTAGE_KV", "auto")
    assert parse_assignment("mv_10kv_min_density_mva_km2=0.1") == ("mv_10kv_min_density_mva_km2", 0.1)
    assert parse_assignment("generator_fallback_kw={lv: 1, mvlv: 2, mv: 3, hvmv: 4}")[1]["hvmv"] == 4.0
    assert parse_assignment("customer_min_mv_kw=300") == ("customer_min_mv_kw", 300.0)
    assert parse_assignment("customer_hall_min_footprint_m2=1500") == ("customer_hall_min_footprint_m2", 1500.0)
    assert parse_assignment("customer_hall_min_footprint_m2=null") == ("customer_hall_min_footprint_m2", None)
    assert parse_assignment("busbar_customer_max_m=500") == ("busbar_customer_max_m", 500.0)
    assert parse_assignment("busbar_customer_max_m=null") == ("busbar_customer_max_m", None)


@pytest.mark.parametrize("text, message", [
    ("MV_STUBS", "KEY=VALUE"),
    ("=none", "KEY=VALUE"),
    ("MV_VARIANT_ID=d2", "use --variant"),
    ("MV_VARIANT_COMMENT=x", "use --comment"),
    ("MV_GRID_GENERATION=True", "pylovo-generate --mv"),
    ("MV_STUBZ=none", "unknown MV parameter"),
    ("MV_STUBS=sometimes", "MV_STUBS must be one of rural, none"),
    ("local_search_rounds=[1", "not a valid YAML value"),
])
def test_parse_assignment_rejects(text, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        parse_assignment(text)


def test_egon_areas_need_egon_substations():
    check_mv_combination({})
    check_mv_combination({"MV_SUBSTATIONS": "egon", "MV_SUPPLY_AREAS": "egon"})
    check_mv_combination({"MV_SUBSTATIONS": "egon", "MV_SUPPLY_AREAS": "routed"})
    with pytest.raises(ValueError, match="MV_SUPPLY_AREAS egon needs MV_SUBSTATIONS egon"):
        check_mv_combination({"MV_SUPPLY_AREAS": "egon"})
    with pytest.raises(ValueError, match="MV_SUPPLY_AREAS egon needs MV_SUBSTATIONS egon"):
        MvParameters(supply_areas="egon")
    assert MvParameters(substations="egon", supply_areas="egon").supply_areas == "egon"
    with pytest.raises(ValueError, match="needs MV_SUBSTATIONS egon"):
        MvParameters().replace_raw({"MV_SUPPLY_AREAS": "egon"})


# ---------------------------------------------------------------------------------------------- MvParameters


def test_one_field_per_spec_and_defaults_equal_the_options():
    fields = {f.name for f in dataclasses.fields(MvParameters) if f.init}
    specs = {spec.field: spec for spec in (*YAML_KEYS.values(), *CONSTANTS.values()) if spec.field}
    assert fields == set(specs)
    params = MvParameters()
    for name, spec in specs.items():
        value = getattr(params, name)
        assert value == spec.default and type(value) is type(spec.default), name
    assert MvParameters().generator_fallback_kw is not MvParameters().generator_fallback_kw   # no shared dicts
    assert (params.variant_id, params.comment, params.vn_kv, params.substations, params.supply_areas,
            params.routing, params.solver, params.max_stations_per_ring, params.stubs, params.overhead_lines,
            params.cable_type, params.overhead_type, params.line_utilization,
            params.max_drop_percent) == ("1", "default", "auto", "osm", "routed", "street", "savings", 20, "rural",
                                         "rural", None, None, 0.6, 5.0)
    assert params.interconnection == "ties"                # normally-open ties between districts by default


def test_construction_coerces_and_the_object_is_frozen():
    params = MvParameters(vn_kv=10, max_stations_per_ring=15.0, hv_mv_sizes_mva=[63, 40])
    assert params.vn_kv == 10.0 and type(params.vn_kv) is float
    assert params.max_stations_per_ring == 15 and type(params.max_stations_per_ring) is int
    assert params.hv_mv_sizes_mva == (40.0, 63.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        params.vn_kv = 20.0
    with pytest.raises(ValueError, match="MV_STUBS must be one of rural, none"):
        MvParameters(stubs="sometimes")


def test_parameters_are_hashable_and_picklable(equipment):
    params = MvParameters(stubs="none")
    assert hash(params) == hash(MvParameters(stubs="none")) and len({params, MvParameters(stubs="none")}) == 1
    assert hash(params) != hash(MvParameters())
    params.check_types()
    copied = pickle.loads(pickle.dumps(params))
    assert copied == params and copied.cable == params.cable and copy.deepcopy(params) == params


def test_replace_validates_and_keeps_the_original():
    params = MvParameters()
    changed = params.replace(stubs="none", local_search_rounds=0, lv_pv_cos_phi=0.95)
    assert (changed.stubs, changed.local_search_rounds, changed.lv_pv_cos_phi) == ("none", 0, 0.95)
    assert params.stubs == "rural" and params == MvParameters()
    with pytest.raises(ValueError, match="unknown MV parameter field"):
        params.replace(MV_STUBS="none")
    with pytest.raises(ValueError, match="unknown MV parameter field"):
        params.replace(_types={})
    with pytest.raises(ValueError, match="MV_MAX_STATIONS_PER_RING must be between 1 and 100"):
        params.replace(max_stations_per_ring=0)
    with pytest.raises(ValueError, match="local_search_rounds must be an integer"):
        params.replace(local_search_rounds=1.5)


def test_replace_raw_takes_yaml_keys_and_constant_names():
    params = MvParameters().replace_raw({"MV_STUBS": "none", "MV_RING_SOLVER": "sweep", "stub_max_kva": 500,
                                         "MV_VARIANT_ID": "sw"})
    assert (params.stubs, params.solver, params.stub_max_kva, params.variant_id) == ("none", "sweep", 500.0, "sw")
    assert MvParameters.from_raw({"MV_NOMINAL_VOLTAGE_KV": 10, "repair_rounds": 2}) == MvParameters(
        vn_kv=10.0, repair_rounds=2)
    with pytest.raises(ValueError, match="execution flag"):
        MvParameters().replace_raw({"MV_GRID_GENERATION": True})
    with pytest.raises(ValueError, match="unknown MV parameter 'stub_max_kw'"):
        MvParameters.from_raw({"stub_max_kw": 1})


def test_types_are_resolved_lazily_and_cached(equipment):
    params = MvParameters()
    assert equipment.calls == []
    assert params.cable == LINE_TYPES["NA2XS2Y 1x240 RM/25 12/20 kV"]
    assert params.overhead == LINE_TYPES["122-AL1/20-ST1A 20.0"]
    assert [t.sn_mva for t in params.hv_mv_types] == [25.0, 31.5, 40.0, 63.0]
    assert [t.interpolated for t in params.hv_mv_types] == [False, True, False, False]
    params.check_types()                                               # auto: the 10 kV types too
    assert params.cable_at(10.0) == LINE_TYPES["NA2XS2Y 1x240 RM/25 6/10 kV"]
    calls = len(equipment.calls)
    params.check_types()
    assert len(equipment.calls) == calls                               # cached
    same = params.replace(stubs="none")
    same.check_types()
    assert len(equipment.calls) == calls                               # unrelated change keeps the types
    ten = params.replace(vn_kv=10)
    assert ten.cable.name == "NA2XS2Y 1x240 RM/25 6/10 kV" and ten.overhead.name == "122-AL1/20-ST1A 10.0"
    assert ten.hv_mv_types[0].vn_lv_kv == 10.0


def test_a_type_of_the_wrong_voltage_or_kind_names_the_key(equipment):
    with pytest.raises(ValueError, match="MV_STANDARD_CABLE must name a pandapower cable line type for 20 kV"):
        _ = MvParameters(vn_kv=20, cable_type="NA2XS2Y 1x240 RM/25 6/10 kV").cable
    with pytest.raises(ValueError, match="MV_OVERHEAD_LINE must name a pandapower overhead line type for 10 kV"):
        MvParameters(overhead_type="70-AL1/11-ST1A 20.0").check_types()        # auto: no 10 kV type of it
    with pytest.raises(ValueError, match="MV_OVERHEAD_LINE must name a pandapower overhead line type"):
        MvParameters(overhead_type="NA2XS2Y 1x240 RM/25 12/20 kV").check_types()
    with pytest.raises(ValueError, match="MV_STANDARD_CABLE .* got 'no such cable'"):
        MvParameters(cable_type="no such cable").to_snapshot()


def test_snapshot_layout(equipment):
    snapshot = MvParameters().to_snapshot()
    assert snapshot == EXPECTED_SNAPSHOT
    assert json_round_trip(snapshot) == snapshot                       # JSON-native, no NaN, no tuples

    def keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from keys(value)
        elif isinstance(node, list):
            for value in node:
                yield from keys(value)
    assert all(re.fullmatch(r"[a-z0-9_]+", key) for key in keys(snapshot))
    assert snapshot["mv_stage_version"] == MV_STAGE_VERSION


def test_snapshot_round_trip(equipment):
    # The historical, narrower conductors remain selectable and round-trip through a stored snapshot.
    params = MvParameters(variant_id="hv", comment="historical conductors").replace_raw({
        "MV_STANDARD_CABLE": "NA2XS2Y 1x150 RM/25 12/20 kV", "MV_OVERHEAD_LINE": "48-AL1/8-ST1A 20.0",
        "MV_STUBS": "none", "MV_INTERCONNECTION": "ties", "local_search_rounds": 0, "hv_mv_sizes_mva": [40, 25],
        "generator_fallback_kw": {"lv": 50, "mvlv": 150, "mv": 5000, "hvmv": 15000}, "lv_pv_cos_phi": 0.95,
        "graph_excluded_klasse": ["Bundesautobahn", "Bundesstraße"], "customer_min_mv_kw": 300,
        "customer_hall_min_footprint_m2": 1500})
    stored = json_round_trip(params.to_snapshot())
    assert (stored["loads"]["customer_min_mv_kw"], stored["loads"]["customer_hall_min_footprint_m2"]) == (300.0,
                                                                                                        1500.0)
    rebuilt = MvParameters.from_snapshot("hv", "historical conductors", stored)
    assert rebuilt == params
    assert rebuilt.to_snapshot() == stored
    assert rebuilt.cable == params.cable and rebuilt.hv_mv_types == params.hv_mv_types
    default = MvParameters()
    assert MvParameters.from_snapshot("1", "default", json_round_trip(default.to_snapshot())) == default
    assert MvParameters.from_snapshot("1", "default", json_round_trip(default.to_snapshot())).cable_type is None


def test_from_snapshot_keeps_the_stored_types(equipment):
    stored = json_round_trip(MvParameters().to_snapshot())
    stored["equipment"]["cable"]["r_ohm_per_km"] = 0.5                 # e.g. a later pandapower table change
    calls = len(equipment.calls)
    rebuilt = MvParameters.from_snapshot("1", "default", stored)
    assert rebuilt.cable.r_ohm_per_km == 0.5 and rebuilt.to_snapshot() == stored
    assert len(equipment.calls) == calls                               # nothing resolved again
    assert rebuilt.replace(stubs="none").cable.r_ohm_per_km == 0.5
    assert rebuilt.replace(vn_kv=10).cable.name == "NA2XS2Y 1x240 RM/25 6/10 kV"
    stored["equipment"]["cable_10kv"]["r_ohm_per_km"] = 0.7          # the 10 kV types are stored as well
    assert MvParameters.from_snapshot("1", "default", stored).cable_at(10.0).r_ohm_per_km == 0.7
    del stored["equipment"]["hv_mv_types_10kv"]
    with pytest.raises(MvStageError, match="resolved equipment"):
        MvParameters.from_snapshot("1", "default", stored)


def test_from_snapshot_fills_values_of_older_snapshots(equipment, monkeypatch):
    stored = json_round_trip(MvParameters(repair_rounds=3, ehv_partner_m=500).to_snapshot())
    del stored["topology"]["repair_rounds"], stored["sources"]["ehv_partner_m"]
    monkeypatch.setitem(MV_ADDED_LATER, ("topology", "repair_rounds"), 1)
    monkeypatch.setitem(MV_ADDED_LATER, ("sources", "ehv_partner_m"), UNKNOWN)
    rebuilt = MvParameters.from_snapshot("1", "default", stored)
    assert rebuilt.repair_rounds == 1                                  # legacy value
    assert rebuilt.ehv_partner_m == CONSTANTS["ehv_partner_m"].default   # unknown legacy: current default


def test_older_fixed_voltage_snapshots_take_the_legacy_threshold(equipment):
    """Snapshots from before MV_NOMINAL_VOLTAGE_KV auto have one fixed voltage and no density threshold."""
    assert MV_ADDED_LATER[("voltage", "mv_10kv_min_density_mva_km2")] == 8.0
    older = json_round_trip(MvParameters(vn_kv=20).to_snapshot())
    del older["voltage"]["mv_10kv_min_density_mva_km2"]
    assert "cable_10kv" not in older["equipment"]
    rebuilt = MvParameters.from_snapshot("d1", "old", older)
    assert rebuilt.vn_kv == 20.0 and rebuilt.mv_10kv_min_density_mva_km2 == 8.0 and rebuilt.voltages == (20.0,)
    assert compare_snapshots(older, json_round_trip(MvParameters(vn_kv=20).to_snapshot()),
                             added_later=MV_ADDED_LATER) == ([], [])
    differences, _ = compare_snapshots(older, json_round_trip(MvParameters().to_snapshot()),
                                       added_later=MV_ADDED_LATER)
    assert "voltage.vn_kv" in differences and "equipment.cable_10kv.name" in differences


def test_generation_mode_defaults_to_load_only_and_legacy_snapshots_remain_full(equipment):
    current = MvParameters()
    assert current.active_cases == ("load", "n1a", "n1b")
    assert current.to_snapshot()["generation"]["include_generation"] is False
    assert current.to_snapshot()["taps"]["rule"] == "load_only"

    full = MvParameters(include_generation=True, cable_type="NA2XS2Y 1x150 RM/25 12/20 kV",
                        overhead_type="48-AL1/8-ST1A 20.0")
    assert full.active_cases == ("load", "n1a", "n1b", "feed_in_wind", "feed_in_pv")
    legacy = json_round_trip(full.to_snapshot())
    del legacy["generation"]["include_generation"]
    legacy["mv_stage_version"] = 3
    restored = MvParameters.from_snapshot("old", "old", legacy)
    assert restored.include_generation is True and restored.active_cases == full.active_cases
    assert restored.cable.name == "NA2XS2Y 1x150 RM/25 12/20 kV"
    assert restored.overhead.name == "48-AL1/8-ST1A 20.0"
    assert restored.to_snapshot()["taps"]["rule"] == "centre_load_feed_in"


def test_customer_rules_of_older_snapshots_take_their_legacy_values(equipment):
    """Before the hall rule, the station-busbar customers and their distance limit (MV_ADDED_LATER)."""
    stored = json_round_trip(MvParameters(vn_kv=20).to_snapshot())
    for key in ("customer_hall_min_footprint_m2", "customer_min_mv_kw", "busbar_customer_max_m"):
        del stored["loads"][key]
    rebuilt = MvParameters.from_snapshot("d1", "old", stored)
    assert (rebuilt.customer_hall_min_footprint_m2, rebuilt.customer_min_mv_kw, rebuilt.busbar_customer_max_m) == (
        None, 0.0, None)
    differences, _ = compare_snapshots(stored, json_round_trip(MvParameters(vn_kv=20).to_snapshot()),
                                       added_later=MV_ADDED_LATER)
    assert differences == ["loads.busbar_customer_max_m", "loads.customer_hall_min_footprint_m2",
                           "loads.customer_min_mv_kw"]
    assert compare_snapshots(stored, json_round_trip(rebuilt.to_snapshot()), added_later=MV_ADDED_LATER) == ([], [])


def test_from_snapshot_rejects_other_layouts(equipment):
    stored = json_round_trip(MvParameters().to_snapshot())
    with pytest.raises(MvStageError, match="schema 2"):
        MvParameters.from_snapshot("1", "default", {**stored, "schema": 2})
    del stored["equipment"]["overhead"]
    with pytest.raises(MvStageError, match="resolved equipment"):
        MvParameters.from_snapshot("1", "default", stored)


def test_from_config_reads_the_shipped_defaults(equipment, monkeypatch):
    from pylovo import config_loader

    assert MvParameters.from_config() == MvParameters()                # shipped values = defaults
    assert len(equipment.calls) == 6                                   # types of both voltages resolved at once
    monkeypatch.setattr(config_loader, "MV_STANDARD_CABLE", "70-AL1/11-ST1A 20.0")
    with pytest.raises(ValueError, match="MV_STANDARD_CABLE"):
        MvParameters.from_config()
    monkeypatch.setattr(config_loader, "MV_STANDARD_CABLE", None)
    monkeypatch.setattr(config_loader, "MV_RING_SOLVER", "sweep")
    monkeypatch.setattr(config_loader, "MV_VARIANT_ID", "sw")
    params = MvParameters.from_config()
    assert (params.solver, params.variant_id) == ("sweep", "sw")


def test_snapshot_with_the_pandapower_types():
    """With the real equipment module: the default types (the most used German MV conductors) and the round trip."""
    try:
        import pylovo.mv_grid.equipment  # noqa: F401
    except Exception as exc:          # not there yet (or not importable)
        pytest.skip(f"pylovo.mv_grid.equipment not available: {exc!r}")
    params = MvParameters()
    cable, overhead = params.cable, params.overhead
    assert cable.name == "NA2XS2Y 1x240 RM/25 12/20 kV" and cable.kind == "cable"
    assert (cable.r_ohm_per_km, cable.x_ohm_per_km, cable.c_nf_per_km, cable.max_i_ka) == pytest.approx(
        (0.122, 0.112, 304.0, 0.421))
    assert overhead.name == "122-AL1/20-ST1A 20.0" and overhead.kind == "overhead"
    assert (overhead.r_ohm_per_km, overhead.x_ohm_per_km, overhead.c_nf_per_km, overhead.max_i_ka) == pytest.approx(
        (0.2376, 0.344, 10.3, 0.41))
    assert sorted(t.sn_mva for t in params.hv_mv_types) == [25.0, 31.5, 40.0, 63.0]
    assert [t.interpolated for t in params.hv_mv_types if t.sn_mva == 31.5] == [True]
    stored = json_round_trip(params.to_snapshot())
    assert MvParameters.from_snapshot("1", "default", stored).to_snapshot() == stored


# ---------------------------------------------------------------------------------------------- snapshots


def test_lv_snapshot_has_no_mv_parameters():
    """The MV parameters live in pylovo.mv_variant: the LV snapshot (and so every LV version) is unchanged."""
    from pylovo.database.preprocessing_mixin import PreprocessingMixin

    snapshot = PreprocessingMixin._generation_parameters_snapshot(object.__new__(PreprocessingMixin))
    paths = version_snapshot._flatten(json_round_trip(snapshot))
    mv_paths = [path for path in paths if any("mv_" in key.lower() for key in path)]
    assert mv_paths == [("cable_dimensioning", "mv_direct_connection_load_threshold_kw")]


def test_compare_snapshots_with_the_mv_legacy_values(equipment):
    stored = json_round_trip(MvParameters().to_snapshot())
    assert compare_snapshots(stored, copy.deepcopy(stored), added_later=MV_ADDED_LATER) == ([], [])
    changed = json_round_trip(MvParameters(stubs="none", hv_mv_sizes_mva=[40, 63]).to_snapshot())
    differences, _ = compare_snapshots(stored, changed, added_later=MV_ADDED_LATER)
    assert differences == ["equipment.hv_mv_types", "equipment.hv_mv_types_10kv", "topology.stubs"]
    # a path added later: its legacy value matches, an unknown legacy value is only reported
    newer = copy.deepcopy(stored)
    newer["topology"]["new_rule"] = 3
    newer["topology"]["other_rule"] = 1
    later = {("topology", "new_rule"): 3, ("topology", "other_rule"): UNKNOWN}
    assert compare_snapshots(stored, newer, added_later=later) == ([], ["topology.other_rule"])
    assert compare_snapshots(stored, newer, added_later=MV_ADDED_LATER)[0] == [
        "topology.new_rule", "topology.other_rule"]


def test_compare_snapshots_keeps_the_lv_default():
    stored = {"power_flow_assessment": {"min_vm_pu": 0.9}}
    expected = {"power_flow_assessment": {"min_vm_pu": 0.9, "lv_reference_voltage_pu": None}}
    assert ("power_flow_assessment", "lv_reference_voltage_pu") in ADDED_LATER
    assert compare_snapshots(stored, expected) == ([], [])
    assert compare_snapshots(stored, expected, added_later=None) == ([], [])
    assert compare_snapshots(stored, expected, added_later=MV_ADDED_LATER)[0] == [
        "power_flow_assessment.lv_reference_voltage_pu"]


# ---------------------------------------------------------------------------------------------- LV version


@pytest.fixture()
def lv_snapshot() -> dict:
    from pylovo.database.preprocessing_mixin import PreprocessingMixin

    return json_round_trip(PreprocessingMixin._generation_parameters_snapshot(object.__new__(PreprocessingMixin)))


def test_lv_parameters_from_the_snapshot_of_this_code(lv_snapshot):
    from pylovo import config_loader

    lv = LvVersionParameters.from_generation_parameters("mv1", lv_snapshot)
    assert lv.version_id == "mv1"
    assert lv.sim_factor == {c: config_loader.SIM_FACTOR[c] for c in CATEGORIES}
    assert list(lv.sim_factor) == list(CATEGORIES)
    assert lv.g().tolist() == [config_loader.SIM_FACTOR[c] for c in CATEGORIES] and lv.g().dtype == np.float64
    assert lv.power_factor == config_loader.DEFAULT_POWER_FACTOR
    assert lv.mv_direct_threshold_kw == config_loader.MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW
    assert lv.lv_reference_voltage_pu == config_loader.LV_REFERENCE_VOLTAGE_PU
    assert (lv.use_dso, lv.use_open, lv.use_manual) == (config_loader.USE_DSO_TRANSFORMER_POSITIONS,
                                                        config_loader.USE_OPEN_TRANSFORMER_POSITIONS,
                                                        config_loader.USE_MANUAL_TRANSFORMER_POSITIONS)
    assert lv.electrical_backend == "pandapower" and lv.residential_only is False
    categories = lv_snapshot["load_calculation"]["consumer_categories"]
    per_m2 = {str(c["definition"]): c["peak_load_per_m2"] for c in categories}
    assert lv.peak_load_per_m2 == {c: per_m2[c] for c in ("Commercial", "Public", "Unknown")}
    data = json_round_trip(lv.as_json())
    assert data == lv.as_json()
    assert LvVersionParameters(**data) == lv and LvVersionParameters.from_json(data) == lv
    assert LvVersionParameters.from_generation_parameters("mv1", json.dumps(lv_snapshot)) == lv   # text column
    legacy = {key: value for key, value in data.items() if key != "peak_load_per_m2"}       # stored before it existed
    assert LvVersionParameters.from_json(legacy).peak_load_per_m2 == {}
    # the station catalogue and planning utilization of the LV version (capacity of the busbar stations)
    assert lv.transformer_planning_utilization == config_loader.TRANSFORMER_PLANNING_UTILIZATION
    assert lv.transformer_mapping == {str(k): sorted(map(float, v)) for k, v in config_loader.TRANSFORMER_MAPPING.items()}
    older = {key: value for key, value in data.items()
             if key not in ("transformer_planning_utilization", "transformer_mapping")}
    old = LvVersionParameters.from_json(older)
    assert old.transformer_planning_utilization == 1.0 and old.transformer_mapping == {}
    assert old.station_ratings(2) == [250.0, 400.0, 630.0]                 # pylovo's default catalogue


def test_lv_parameters_fallbacks(lv_snapshot, caplog):
    snapshot = copy.deepcopy(lv_snapshot)
    expected = LvVersionParameters.from_generation_parameters("v", snapshot)
    del snapshot["load_calculation"]["sim_factor"]                     # older layout: categories only
    del snapshot["electrical_backend"], snapshot["power_flow_assessment"]["lv_reference_voltage_pu"]
    del snapshot["transformer_placement"]["use_manual_transformer_positions"]
    snapshot["residential_only_generation"] = True
    with caplog.at_level(logging.WARNING):
        lv = LvVersionParameters.from_generation_parameters("v", snapshot)
    assert lv.sim_factor == expected.sim_factor and lv.electrical_backend == "pandapower"
    assert lv.lv_reference_voltage_pu is None and lv.use_manual is False and lv.residential_only is True
    assert "no MV customers" in caplog.text


@pytest.mark.parametrize("change, message", [
    (lambda s: s.clear(), "no sim factor for Residential, Commercial, Public, Unknown"),
    (lambda s: (s["load_calculation"].pop("sim_factor"),
                s["load_calculation"]["consumer_categories"].pop()), "no sim factor for"),
    (lambda s: s["load_calculation"]["sim_factor"].update(Public=1.5), "sim factor of Public"),
    (lambda s: s["load_calculation"].pop("default_power_factor"), "default power factor"),
    (lambda s: s["load_calculation"].update(default_power_factor=0), "default power factor"),
    (lambda s: s["cable_dimensioning"].pop("mv_direct_connection_load_threshold_kw"), "MV-direct connection"),
    (lambda s: [c.update(peak_load_per_m2=None) for c in s["load_calculation"]["consumer_categories"]
                if c["definition"] == "Commercial"], "no peak load per m² for Commercial"),
    (lambda s: [c.update(peak_load_per_m2=0.0) for c in s["load_calculation"]["consumer_categories"]
                if c["definition"] == "Public"], "peak load per m² of Public"),
    (lambda s: s.update(electrical_backend="opendss"), "pandapower backend"),
    (lambda s: s["power_flow_assessment"].update(lv_reference_voltage_pu="0.96"), "LV reference voltage"),
])
def test_lv_parameters_reject_incomplete_snapshots(lv_snapshot, change, message):
    snapshot = copy.deepcopy(lv_snapshot)
    change(snapshot)
    with pytest.raises(MvStageError, match=re.escape(message)):
        LvVersionParameters.from_generation_parameters("v", snapshot)


def test_lv_station_ratings_by_settlement_type():
    lv = LvVersionParameters(version_id="v", sim_factor={c: 0.5 for c in CATEGORIES}, power_factor=0.95,
                             mv_direct_threshold_kw=200.0, residential_only=False, lv_reference_voltage_pu=0.96,
                             use_dso=False, use_open=False, use_manual=False, electrical_backend="pandapower",
                             transformer_planning_utilization=0.8,
                             transformer_mapping={"1": [100.0, 160.0, 250.0, 400.0], "3": [400.0, 630.0, 800.0]})
    assert lv.station_ratings(1) == [100.0, 160.0, 250.0, 400.0] and lv.station_ratings(3.0) == [400.0, 630.0, 800.0]
    assert lv.station_ratings(2) == lv.station_ratings(None) == lv.station_ratings(float("nan")) == [
        100.0, 160.0, 250.0, 400.0, 630.0, 800.0]                          # unknown type: the whole catalogue
    assert LvVersionParameters.from_json(json_round_trip(lv.as_json())) == lv


def test_lv_parameters_need_a_snapshot():
    with pytest.raises(MvStageError, match="has no generation parameter snapshot"):
        LvVersionParameters.from_generation_parameters("v", None)


# ---------------------------------------------------------------------------------------------- contracts


def summary(**changes) -> MvRunSummary:
    values = dict(version_id="mv1", variant_id="d1", status="done", plz_requested=[1, 2], plz_used=[1, 2],
                  plz_missing=[], grids_total=42, grids_done=42, grids_failed=0, grids_pending=0, n_stations=4903,
                  n_customers=61, n_generators=37, circuit_km=4230.4, cable_share=0.66, n_violation_grids=2,
                  n_band_conflicts=118, warnings=[], elapsed_s=312.0)
    values.update(changes)
    return MvRunSummary(**values)


def test_run_summary_lines():
    text = summary().describe()
    assert text == ("✓ MV variant d1 of version mv1: 42 grids (42 done), 4,903 stations, 61 customers, "
                    "37 generators, 4,230 km (cable 66 %), 2 grids with violations, 118 band conflicts, 312 s, "
                    "generation included planning")
    partial = summary(status="partial", grids_done=39, grids_failed=1, grids_pending=2, plz_missing=[3],
                      warnings=["ties not implemented"]).describe().splitlines()
    assert partial[0].startswith("✗ MV variant d1 of version mv1: 42 grids (39 done, 1 failed, 2 pending)")
    assert partial[1:] == ["✗ 1 requested PLZ have no LV grids in version mv1: 3", "✗ ties not implemented"]
    load_only = summary(active_cases=("load", "n1a", "n1b"))
    assert "load-only planning" in load_only.describe()
    assert load_only.to_dict()["active_cases"] == ["load", "n1a", "n1b"]


def test_run_summary_prints_voltages_ties_and_the_demand_anchor():
    ties = {"n_ties": 12, "tie_km": 15.31, "n_rings": 30, "n_rings_with_tie": 12, "ring_share_with_tie": 0.4,
            "takeover_checked": 24, "takeover_ok": 20, "tie_max_length_m": 2000.0}
    demand = {"kw_per_inhabitant": 1.234, "coincident_mw": 98.7, "population": 80000, "n_plz": 6,
              "customer_mw": 20.0, "n_customer_nodes": 40, "busbar_customer_mw": 3.0, "approximate": False}
    voltages = {"rule": "auto", "threshold_mva_km2": 8.0, "grids": {"20": 3, "10": 1}}
    lines = summary(ties=ties, demand=demand, voltages=voltages).describe().splitlines()
    assert lines[1] == "✓ Nominal voltage auto (10 kV from 8 MVA/km² installed): 3 at 20 kV, 1 at 10 kV"
    assert lines[2] == ("✓ Ties: 12 normally-open ties, 15.3 km, 12 of 30 rings with a tie (40 %), takeover within "
                        "the (n-1) limits at 20 of 24 tie ends")
    assert lines[3].startswith("✓ Demand anchor: 1.23 kW per inhabitant (98.7 MW coincident, 80,000 inhabitants")
    approximate = {**demand, "approximate": True, "partial_plz": {"12345": 0.4}}
    line = summary(demand=approximate).describe().splitlines()[1]
    assert line.startswith("✗ Demand anchor: 1.23 kW per inhabitant, approximate (98.7 MW")
    assert line.endswith("PLZ only partly in the region or without population: 12345 (40 % of its area)")
    none = summary(ties={"n_ties": 0, "tie_max_length_m": 2000.0}, voltages={"rule": "fixed", "grids": {"20": 4}})
    assert none.describe().splitlines()[1:] == ["✓ Nominal voltage: 4 at 20 kV",
                                                "✓ Ties: none (no rings of different districts of the same "
                                                "voltage within 2 km)"]
    assert json_round_trip(summary(ties=ties, voltages=voltages).to_dict())["voltages"]["grids"] == {"20": 3, "10": 1}


def test_run_summary_dict_is_json_native():
    data = summary(n_stations=np.int64(5), circuit_km=np.float64(1.5), plz_used=np.array([1, 2])).to_dict()
    assert json_round_trip(data) == data
    assert data["n_stations"] == 5 and type(data["n_stations"]) is int and data["plz_used"] == [1, 2]
