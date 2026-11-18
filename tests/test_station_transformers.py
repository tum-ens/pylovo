"""The units of a station rating and the data of each unit (pylovo.station_transformers)."""
import pandapower as pp
import pytest

from pylovo import station_transformers as st
from pylovo.electrical_backend.core.specs import BusSpec, ExtGridSpec, TransformerSpec
from pylovo.electrical_backend.pandapower.backend import PandapowerBackend
from pylovo.mv_grid import coupling, electrical


@pytest.mark.parametrize("rating, units", [
    (100.0, (100.0, 1)), (400.0, (400.0, 1)), (630.0, (630.0, 1)),
    (800.0, (800.0, 1)),                       # a standard size: one unit, not 2 x 400 kVA
    (500.0, (500.0, 1)), (315.0, (315.0, 1)),  # off the type list: one unit of its own rating
    (1260.0, (630.0, 2)), (1890.0, (630.0, 3)),  # above the catalogue: n x 630 kVA
    (1000.0, (1000.0, 1)), (1600.0, (800.0, 2)),
])
def test_station_units_total_the_rating(rating, units):
    assert st.station_units(rating) == units
    assert units[0] * units[1] == pytest.approx(rating)


def test_the_pandapower_sizes_are_pandapower_types_and_the_small_ones_are_derived():
    types = pp.create_empty_network().std_types["trafo"]
    for kva in (250, 400, 630):
        name, data = st.unit_type(kva)
        assert data == types[name]
    name, data = st.unit_type(160)
    base = types["0.25 MVA 20/0.4 kV"]
    assert name == "0.16 MVA 20/0.4 kV"
    assert data == {**base, "sn_mva": 0.16, "pfe_kw": base["pfe_kw"] * 0.16 / 0.25}


def test_the_800_kva_unit_has_the_ecodesign_tier_2_losses():
    name, data = st.unit_type(800)
    assert name == "0.8 MVA 20/0.4 kV" and data["sn_mva"] == 0.8 and data["vk_percent"] == 6.0
    assert data["vkr_percent"] / 100 * 800 == pytest.approx(6.0)     # load losses 6.0 kW
    assert data["pfe_kw"] == 0.585


def test_a_unit_without_a_type_takes_the_next_larger_one_with_scaled_no_load_losses():
    _, data = st.unit_type(750)
    _, base = st.unit_type(800)
    assert data == {**base, "sn_mva": 0.75, "pfe_kw": base["pfe_kw"] * 0.75 / 0.8}
    _, above = st.unit_type(1000)                                    # above the largest type: the largest one
    assert above["vk_percent"] == base["vk_percent"] and above["sn_mva"] == 1.0


def test_lv_generation_the_mv_budget_and_the_coupling_build_the_same_units():
    for rating in (160.0, 400.0, 800.0, 1260.0):
        unit_kva, parallel = st.station_units(rating)
        backend = PandapowerBackend()
        backend.initialize_circuit(name="t", source_bus="MVbus 1", primary_kv=20.0)
        for spec in (BusSpec(name="MVbus 1", voltage_kv=20.0), BusSpec(name="LVbus 1", voltage_kv=0.4),
                     ExtGridSpec(name="External grid", bus="MVbus 1", vm_pu=1.0),
                     TransformerSpec(name="T", bus1="MVbus 1", bus2="LVbus 1", kva=unit_kva, parallel=parallel)):
            backend.create_component(spec)
        trafo = backend.net.trafo.iloc[0]
        assert (trafo.sn_mva * 1000 * trafo.parallel) == pytest.approx(rating)
        assert electrical.station_transformer(rating) == (trafo.vk_percent, trafo.vkr_percent)
        upsized = coupling.upsize_station_transformer(backend.net, rating).trafo.iloc[0]
        for column in ("sn_mva", "vk_percent", "vkr_percent", "pfe_kw", "i0_percent", "parallel", "std_type"):
            assert upsized[column] == trafo[column], (rating, column)
