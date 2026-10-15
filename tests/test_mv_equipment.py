"""MV equipment (pylovo.mv_grid.equipment): pandapower line and HV/MV transformer types, mixed lines, sizing."""
from __future__ import annotations

import dataclasses
import json
import math

import pandapower as pp
import pytest

from pylovo.mv_grid import equipment
from pylovo.mv_grid.model import HvMvType, LineType
from pylovo.mv_grid.parameters import MvParameters

SIZES = (25.0, 31.5, 40.0, 63.0)

#: pandapower 3.4.0 values (DESIGN section 4.6): r, x (Ohm/km), c (nF/km), max_i (kA).
LINE_VALUES = {
    "NA2XS2Y 1x240 RM/25 12/20 kV": ("cable", 20.0, 0.122, 0.112, 304.0, 0.421),
    "122-AL1/20-ST1A 20.0": ("overhead", 20.0, 0.2376, 0.344, 10.3, 0.41),
    "NA2XS2Y 1x240 RM/25 6/10 kV": ("cable", 10.0, 0.122, 0.105, 456.0, 0.416),
    "122-AL1/20-ST1A 10.0": ("overhead", 10.0, 0.2376, 0.323, 11.1, 0.41),
    "NA2XS2Y 1x150 RM/25 12/20 kV": ("cable", 20.0, 0.206, 0.116, 250.0, 0.319),     # defaults: the most used
    "48-AL1/8-ST1A 20.0": ("overhead", 20.0, 0.5939, 0.372, 9.5, 0.21),               # types in German MV grids
    "NA2XS2Y 1x150 RM/25 6/10 kV": ("cable", 10.0, 0.206, 0.11, 360.0, 0.315),
    "48-AL1/8-ST1A 10.0": ("overhead", 10.0, 0.5939, 0.35, 10.1, 0.21),
    "70-AL1/11-ST1A 20.0": ("overhead", 20.0, 0.4132, 0.36, 9.7, 0.29),
}


def test_line_types_come_from_pandapower():
    for name, (kind, vn_kv, r, x, c, max_i) in LINE_VALUES.items():
        lt = equipment.line_type(name)
        assert lt == LineType(name, kind, vn_kv, r, x, c, max_i)
        assert all(type(getattr(lt, f)) is float for f in ("vn_kv", "r_ohm_per_km", "x_ohm_per_km", "c_nf_per_km",
                                                            "max_i_ka"))
        equipment.check_line_type(lt, vn_kv, kind)
    assert equipment.default_line_types(20) == ("NA2XS2Y 1x150 RM/25 12/20 kV", "48-AL1/8-ST1A 20.0")
    assert equipment.default_line_types(10.0) == ("NA2XS2Y 1x150 RM/25 6/10 kV", "48-AL1/8-ST1A 10.0")
    for vn_kv, names in equipment.DEFAULT_LINE_TYPES.items():          # the defaults fit their voltage
        for name, kind in zip(names, ("cable", "overhead")):
            equipment.check_line_type(equipment.line_type(name), vn_kv, kind)
    with pytest.raises(ValueError, match="20 and 10 kV only"):
        equipment.default_line_types(15)


def test_rated_voltage_from_the_type_name():
    assert equipment.rated_kv("NA2XS2Y 1x95 RM/25 12/20 kV") == 20.0
    assert equipment.rated_kv("NA2XS2Y 1x185 RM/25 6/10 kV") == 10.0
    assert equipment.rated_kv("N2XS(FL)2Y 1x120 RM/35 64/110 kV") == 110.0
    assert equipment.rated_kv("48-AL1/8-ST1A 20.0") == 20.0
    assert equipment.rated_kv("15-AL1/3-ST1A 0.4") == 0.4
    assert equipment.rated_kv("NAYY 4x50 SE") is None


def test_unknown_and_unsuitable_line_types_are_rejected():
    with pytest.raises(ValueError, match="not a pandapower line type .*closest: NA2XS2Y 1x240 RM/25 12/20 kV"):
        equipment.line_type("NA2XS2Y 1x240 RM/25 12/20kV")
    with pytest.raises(ValueError, match="names no rated voltage"):
        equipment.line_type("NAYY 4x50 SE")
    cable = equipment.line_type("NA2XS2Y 1x240 RM/25 6/10 kV")
    overhead = equipment.line_type("122-AL1/20-ST1A 20.0")
    with pytest.raises(ValueError, match="rated for 10 kV, not for 20 kV"):
        equipment.check_line_type(cable, 20, "cable")
    with pytest.raises(ValueError, match="overhead type, not a cable type"):
        equipment.check_line_type(overhead, 20, "cable")
    with pytest.raises(ValueError, match="cable or overhead"):
        equipment.check_line_type(overhead, 20, "mixed")
    with pytest.raises(ValueError, match="rated for 0.4 kV"):
        equipment.check_line_type(equipment.line_type("15-AL1/3-ST1A 0.4"), 20, "overhead")


def test_mv_parameters_resolve_and_check_the_types():
    params = MvParameters()
    assert params.cable == equipment.line_type("NA2XS2Y 1x150 RM/25 12/20 kV")
    assert params.overhead == equipment.line_type("48-AL1/8-ST1A 20.0")
    assert [t.name for t in params.hv_mv_types] == ["25 MVA 110/20 kV", "31.5 MVA 110/20 kV", "40 MVA 110/20 kV",
                                                     "63 MVA 110/20 kV"]
    heavier = MvParameters().replace_raw({"MV_STANDARD_CABLE": "NA2XS2Y 1x240 RM/25 12/20 kV",   # the former
                                          "MV_OVERHEAD_LINE": "122-AL1/20-ST1A 20.0"})          # defaults
    assert (heavier.cable.max_i_ka, heavier.overhead.max_i_ka) == (0.421, 0.41)
    assert (heavier.cable_type, heavier.overhead_type) == ("NA2XS2Y 1x240 RM/25 12/20 kV", "122-AL1/20-ST1A 20.0")
    ten = MvParameters(vn_kv=10)
    assert (ten.cable.vn_kv, ten.overhead.vn_kv) == (10.0, 10.0)
    assert {t.vn_lv_kv for t in ten.hv_mv_types} == {10.0}
    with pytest.raises(ValueError, match="MV_STANDARD_CABLE must name a pandapower cable line type for 20 kV"):
        _ = MvParameters(cable_type="122-AL1/20-ST1A 20.0").cable
    with pytest.raises(ValueError, match="MV_OVERHEAD_LINE .* rated for 10 kV"):
        _ = MvParameters(overhead_type="122-AL1/20-ST1A 10.0").overhead
    with pytest.raises(ValueError, match="hv_mv_sizes_mva .* no HV/MV transformer type of 16 MVA"):
        _ = MvParameters(hv_mv_sizes_mva=[16, 25]).hv_mv_types
    snapshot = json.loads(json.dumps(params.to_snapshot()))
    assert [t["interpolated"] for t in snapshot["equipment"]["hv_mv_types"]] == [False, True, False, False]
    assert MvParameters.from_snapshot("1", "default", snapshot).to_snapshot() == snapshot


# ---------------------------------------------------------------------------------------------- line parameters
def test_mixed_lines_are_length_weighted():
    cable, overhead = equipment.line_type("NA2XS2Y 1x240 RM/25 12/20 kV"), equipment.line_type("122-AL1/20-ST1A 20.0")
    mixed = equipment.mixed_parameters(cable, overhead, 300.0, 700.0)
    assert mixed["r_ohm_per_km"] == pytest.approx(0.3 * 0.122 + 0.7 * 0.2376)
    assert mixed["x_ohm_per_km"] == pytest.approx(0.3 * 0.112 + 0.7 * 0.344)
    assert mixed["c_nf_per_km"] == pytest.approx(0.3 * 304.0 + 0.7 * 10.3)
    assert mixed["max_i_ka"] == 0.41                                     # the weaker part limits the line
    only_cable = equipment.mixed_parameters(cable, overhead, 250.0, 0.0)
    assert only_cable == pytest.approx({"r_ohm_per_km": 0.122, "x_ohm_per_km": 0.112, "c_nf_per_km": 304.0,
                                        "max_i_ka": 0.421})
    for lengths in ((0.0, 0.0), (-1.0, 5.0), (math.nan, 5.0), (5.0, math.inf)):
        with pytest.raises(ValueError, match="mixed line"):
            equipment.mixed_parameters(cable, overhead, *lengths)


def test_line_parameters_and_names_by_kind():
    cable, overhead = equipment.line_type("NA2XS2Y 1x240 RM/25 12/20 kV"), equipment.line_type("122-AL1/20-ST1A 20.0")
    assert equipment.line_parameters("cable", cable, overhead)["max_i_ka"] == 0.421
    assert equipment.line_parameters("overhead", cable, overhead)["x_ohm_per_km"] == 0.344
    assert equipment.line_parameters("mixed", cable, overhead, 500.0, 500.0) == equipment.mixed_parameters(
        cable, overhead, 500.0, 500.0)
    assert equipment.line_std_type_name("cable", cable, overhead) == cable.name
    assert equipment.line_std_type_name("overhead", cable, overhead) == overhead.name
    assert equipment.line_std_type_name("mixed", cable, overhead) == (
        "NA2XS2Y 1x240 RM/25 12/20 kV + 122-AL1/20-ST1A 20.0")
    with pytest.raises(ValueError, match="cable, overhead, mixed"):
        equipment.line_parameters("tie", cable, overhead)
    with pytest.raises(ValueError, match="cable, overhead, mixed"):
        equipment.line_std_type_name("ring", cable, overhead)


def test_thermal_rating_and_parallel_circuits():
    cable = equipment.line_type("NA2XS2Y 1x240 RM/25 12/20 kV")
    assert equipment.thermal_mva(cable) == pytest.approx(14.584, abs=1e-3)             # Appendix A: 14.6 MVA
    assert equipment.thermal_mva(equipment.line_type("NA2XS2Y 1x240 RM/25 6/10 kV")) == pytest.approx(7.205, abs=1e-3)
    assert equipment.parallel_count(5.5, cable, 0.6, 2) == (1, False)                   # dedicated feeder limit
    assert equipment.parallel_count(10.0, cable, 0.6, 2) == (2, False)
    assert equipment.parallel_count(20.0, cable, 0.6, 2) == (2, True)                   # flag dedicated_overload
    assert equipment.parallel_count(equipment.thermal_mva(cable), cable, 1.0, 2) == (1, False)   # feed-in at 100 %
    assert equipment.parallel_count(0.0, cable, 0.6, 2) == (1, False)
    assert equipment.parallel_count(math.nan, cable, 0.6, 2) == (1, False)
    default = equipment.line_type("NA2XS2Y 1x150 RM/25 12/20 kV")
    assert equipment.thermal_mva(default) == pytest.approx(11.051, abs=1e-3)
    assert equipment.parallel_count(5.5 / 0.95, default, 0.6, 2) == (1, False)      # 5.5 MW still on one cable
    with pytest.raises(ValueError, match="positive"):
        equipment.parallel_count(1.0, cable, 0.0, 2)


# ---------------------------------------------------------------------------------------------- HV/MV transformers
def test_hv_mv_types_with_the_interpolated_31_5_mva_type():
    types = equipment.hv_mv_types(20.0, SIZES)
    assert [t.name for t in types] == ["25 MVA 110/20 kV", "31.5 MVA 110/20 kV", "40 MVA 110/20 kV",
                                       "63 MVA 110/20 kV"]
    assert [t.interpolated for t in types] == [False, True, False, False]
    library = pp.create_empty_network().std_types["trafo"]
    for t in (types[0], types[2], types[3]):
        data = library[t.name]
        for key in ("sn_mva", "vn_hv_kv", "vn_lv_kv", "vk_percent", "vkr_percent", "pfe_kw", "i0_percent",
                    "shift_degree", "vector_group", "tap_side", "tap_neutral", "tap_min", "tap_max",
                    "tap_step_percent"):
            assert getattr(t, key) == data[key], (t.name, key)
    t31 = types[1]
    assert (t31.sn_mva, t31.vk_percent, t31.vkr_percent, t31.pfe_kw, t31.i0_percent) == (31.5, 14.0, 0.38, 16.0, 0.06)
    rest = dataclasses.replace(types[2], name=t31.name, sn_mva=31.5, vk_percent=14.0, vkr_percent=0.38, pfe_kw=16.0,
                               i0_percent=0.06, interpolated=True)
    assert t31 == rest                                                   # the rest as 40 MVA (YNd5, tap +-9 x 1.5 %)
    assert (t31.vector_group, t31.tap_min, t31.tap_max, t31.tap_step_percent) == ("YNd5", -9, 9, 1.5)
    assert [t.sn_mva for t in equipment.hv_mv_types(20, (63, 25))] == [63.0, 25.0]


def test_hv_mv_types_at_10_kv_and_unknown_sizes():
    ten = equipment.hv_mv_types(10, SIZES)
    assert [t.name for t in ten] == ["25 MVA 110/10 kV", "31.5 MVA 110/10 kV", "40 MVA 110/10 kV",
                                     "63 MVA 110/10 kV"]
    assert all(t.vn_lv_kv == 10.0 and t.vn_hv_kv == 110.0 for t in ten)
    for size in (16, 50, 100):
        with pytest.raises(ValueError, match=rf"no HV/MV transformer type of {size} MVA for 110/20 kV "
                                             r"\(available: 25, 31.5, 40, 63 MVA\)"):
            equipment.hv_mv_types(20, (size,))
    with pytest.raises(ValueError, match="no HV/MV transformer types for 110/15 kV"):
        equipment.hv_mv_types(15, (25,))


def test_hv_mv_std_type_works_in_a_pandapower_net():
    """The resolved type (here 31.5 MVA) is a pandapower type with an active ratio tap changer on the HV side."""
    (t31,) = equipment.hv_mv_types(20, (31.5,))
    net = pp.create_empty_network()
    assert equipment.add_hv_mv_std_type(net, t31) == "31.5 MVA 110/20 kV"
    hv = pp.create_bus(net, 110.0)
    mv = pp.create_bus(net, 20.0)
    pp.create_ext_grid(net, hv, vm_pu=1.0)
    units = [pp.create_transformer(net, hv, mv, std_type=t31.name, name=f"HV/MV transformer {i}") for i in (1, 2)]
    pp.create_load(net, mv, p_mw=30.0, q_mvar=30.0 * 0.329)
    assert net.trafo.loc[units[0], ["sn_mva", "vk_percent", "vkr_percent", "pfe_kw", "i0_percent"]].tolist() == [
        31.5, 14.0, 0.38, 16.0, 0.06]
    pp.runpp(net)
    neutral = net.res_bus.vm_pu.at[mv]
    net.trafo["tap_pos"] = -3                                            # a negative HV tap raises the MV side
    pp.runpp(net)
    raised = net.res_bus.vm_pu.at[mv]
    assert 0.95 < neutral < 1.0
    assert raised - neutral == pytest.approx(3 * 0.015, abs=0.006)
    replaced = dataclasses.replace(equipment.hv_mv_types(20, (25,))[0], vk_percent=11.0)   # e.g. a snapshot value
    equipment.add_hv_mv_std_type(net, replaced)
    assert net.std_types["trafo"]["25 MVA 110/20 kV"]["vk_percent"] == 11.0
    assert pp.create_empty_network().std_types["trafo"]["25 MVA 110/20 kV"]["vk_percent"] == 12   # library intact


# ---------------------------------------------------------------------------------------------- sizing
def test_hv_mv_sizing_examples():
    assert equipment.size_hv_mv(10.0, 0.0, SIZES, 2, 1.0) == (2, 25.0, "sized")
    assert equipment.size_hv_mv(30.0, 0.0, SIZES, 2, 1.0) == (2, 31.5, "sized")
    assert equipment.size_hv_mv(70.0, 0.0, SIZES, 2, 1.0) == (3, 63.0, "large_substation")
    assert equipment.size_hv_mv(10.0, 0.0, SIZES, 2, 1.0, known_units_mva=(40.0, 40.0)) == (2, 40.0, "osm_rating")


def test_hv_mv_sizing_rules():
    assert equipment.size_hv_mv(10.0, 30.0, SIZES, 2, 1.0) == (2, 31.5, "sized")         # feed-in decides
    assert equipment.size_hv_mv(25.0, 0.0, SIZES, 2, 1.0) == (2, 25.0, "sized")          # exactly (n-1) secure
    assert equipment.size_hv_mv(25.0, 0.0, SIZES, 2, 0.8) == (2, 31.5, "sized")
    assert equipment.size_hv_mv(70.0, 0.0, SIZES, 3, 1.0) == (3, 40.0, "sized")
    assert equipment.size_hv_mv(126.0, 0.0, SIZES, 2, 1.0) == (3, 63.0, "large_substation")
    assert equipment.size_hv_mv(127.0, 0.0, SIZES, 2, 1.0) == (4, 63.0, "large_substation")
    assert equipment.size_hv_mv(30.0, 0.0, SIZES, 1, 1.0) == (1, 31.5, "sized")          # one unit: no reserve
    assert equipment.size_hv_mv(70.0, 0.0, SIZES, 1, 1.0) == (2, 63.0, "large_substation")
    assert equipment.size_hv_mv(0.0, 0.0, SIZES, 2, 1.0) == (2, 25.0, "sized")
    assert equipment.size_hv_mv(30.0, math.nan, [63.0, 25.0], 2, 1.0) == (2, 63.0, "sized")   # unknown feed-in
    assert equipment.size_hv_mv(30.0, None, SIZES, 2, 1.0) == (2, 31.5, "sized")
    # known ratings: two or more are kept as built (the smallest one counts), a single one is not enough
    assert equipment.size_hv_mv(60.0, 0.0, SIZES, 2, 1.0, (40.0, 63.0, 40.0)) == (3, 40.0, "osm_rating")
    assert equipment.size_hv_mv(30.0, 0.0, SIZES, 2, 1.0, (40.0,)) == (2, 31.5, "sized")
    assert equipment.size_hv_mv(30.0, 0.0, SIZES, 2, 1.0, (40.0, None, math.nan, 0.0)) == (2, 31.5, "sized")
    with pytest.raises(ValueError, match="at least one positive transformer size"):
        equipment.size_hv_mv(10.0, 0.0, (), 2, 1.0)
    with pytest.raises(ValueError, match="units >= 1"):
        equipment.size_hv_mv(10.0, 0.0, SIZES, 0, 1.0)
    with pytest.raises(ValueError, match="positive utilization"):
        equipment.size_hv_mv(10.0, 0.0, SIZES, 2, 0.0)


def test_hv_mv_type_records_are_json_native():
    for t in equipment.hv_mv_types(20, SIZES):
        assert isinstance(t, HvMvType)
        assert all(type(getattr(t, f)) is int for f in ("tap_neutral", "tap_min", "tap_max"))
        assert all(type(getattr(t, f)) is float for f in ("sn_mva", "vk_percent", "vkr_percent", "pfe_kw",
                                                           "i0_percent", "shift_degree", "tap_step_percent"))
        assert json.loads(json.dumps(dataclasses.asdict(t))) == dataclasses.asdict(t)
