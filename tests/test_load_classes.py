"""Load classes of non-residential buildings and connection levels (no database): the benchmark conversion, the checks
of LOAD_CLASSES, the SQL of the class loads, the snapshot and the old model's SQL (frozen versions)."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math

import pytest

from pylovo import config_loader, load_classes
from pylovo.database import preprocessing_mixin
from pylovo.database.preprocessing_mixin import PreprocessingMixin
from pylovo.utils import category_simultaneous_load
from pylovo.version_snapshot import compare_snapshots

SIM = {"Commercial": 0.5, "Public": 0.6, "Residential": 0.07, "Unknown": 0.6}


def shipped():
    return copy.deepcopy(dict(config_loader.CONFIG_GENERATION["LOAD_CLASSES"]))


def table(config=None):
    return load_classes.load_class_table(shipped() if config is None else config, SIM)


def row(name, config=None):
    t = table(config)
    return t[t["load_class"] == name].iloc[0].to_dict()


# --- the shipped classes ------------------------------------------------------------------------------------------
def test_the_shipped_classes_cover_the_infdb_classes_and_name_their_sources():
    t = table()
    assert list(t["load_class"]) == list(load_classes.INFDB_LOAD_CLASSES)
    for record in t.to_dict(orient="records"):
        assert "BMWi/BMUB 2015" in record["source"] and "BDEW" in record["source"], record["load_class"]
        assert record["load_profile"] in record["source"]


def test_the_shipped_model_is_the_one_of_the_frozen_versions():
    # new behaviour only with a new VERSION_ID: the shipped configuration keeps the W/m² per use and no levels
    assert config_loader.NONRESIDENTIAL_LOAD_MODEL == "building_use"
    assert config_loader.LOAD_CLASS_TABLE is None
    assert config_loader.CONNECTION_LEVEL_THRESHOLDS_KW is None
    assert config_loader.UNCLASSIFIED_LOAD_CLASS is None


def test_the_benchmark_becomes_annual_and_coincident_load_per_m2_of_gross_floor_area():
    office = row("office")
    # BMWi/BMUB 2015 Tab. 2.2 Nr. 9.1: 35 kWh/(m² NGF a); NGF = 0.85 BGF; BDEW G1 2084 full-load hours
    assert office["yearly_consumption_per_m2"] == pytest.approx(35 * 0.85)
    assert office["coincident_load_per_m2"] == pytest.approx(35 * 0.85 / 2084 * 1000)
    assert load_classes.own_peak_per_m2(office["coincident_load_per_m2"], 0.5) == pytest.approx(28.55, abs=0.01)
    production = row("production")
    assert production["large_from_m2"] == pytest.approx(3500 / 0.85)
    assert production["large_coincident_load_per_m2"] == pytest.approx(65 * 0.85 / 4191 * 1000)
    assert math.isnan(office["large_from_m2"])


def test_agriculture_and_warehouses_stay_far_below_the_former_commercial_load():
    former = 79.0                                                   # W/m² of every Commercial building before
    for name in ("agriculture", "warehouse"):
        r = row(name)
        for coincident in (r["coincident_load_per_m2"], r["large_coincident_load_per_m2"]):
            if not math.isnan(coincident):
                assert load_classes.own_peak_per_m2(coincident, SIM["Commercial"]) < former / 2


def test_the_own_peak_gives_the_profile_peak_for_many_buildings():
    # pylovo's law for n buildings of one class tends to n x the coincident load of the profile
    r = row("retail")
    own = load_classes.own_peak_per_m2(r["coincident_load_per_m2"], SIM["Commercial"])
    n = 10**8
    assert category_simultaneous_load(n * own, n, SIM["Commercial"]) / n == pytest.approx(r["coincident_load_per_m2"],
                                                                                          rel=1e-4)
    # and the own peak for one building
    assert category_simultaneous_load(own, 1, SIM["Commercial"]) == pytest.approx(own)


def test_the_value_for_large_buildings_applies_above_its_net_floor_area():
    r = row("food_retail")
    limit = 300 / 0.85
    small = load_classes.class_load_kw(r, limit, "Commercial", SIM)
    large = load_classes.class_load_kw(r, limit + 1, "Commercial", SIM)
    assert small == pytest.approx(limit * 75 * 0.85 / 4370 / 0.5)
    assert large == pytest.approx((limit + 1) * 265 * 0.85 / 4370 / 0.5)


def test_a_class_without_category_counts_in_the_use_of_the_building():
    other = row("other")
    assert other["consumer_category"] is None
    assert load_classes.class_load_kw(other, 1000, "Public", SIM) == pytest.approx(
        1000 * other["coincident_load_per_m2"] / 0.6 / 1000)
    assert load_classes.class_load_kw(other, 1000, "Commercial", SIM) == pytest.approx(
        1000 * other["coincident_load_per_m2"] / 0.5 / 1000)
    # a class with a category ignores the use of the building
    school = row("education")
    assert load_classes.class_load_kw(school, 1000, "Commercial", SIM) == load_classes.class_load_kw(
        school, 1000, "Public", SIM)


# --- the checks of LOAD_CLASSES -----------------------------------------------------------------------------------
def changed(path, value):
    config = shipped()
    node = config
    for key in path[:-1]:
        node = node[key]
    if value is KeyError:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return config


@pytest.mark.parametrize("path, value, message", [
    (("net_floor_area_factor",), 1.2, "net_floor_area_factor must be at most 1"),
    (("net_floor_area_factor",), 0, "net_floor_area_factor must be a number above 0"),
    (("load_profiles", "G1"), 9000, "full-load hours of G1 must be at most 8784"),
    (("classes", 0, "load_profile"), "G9", "load_profile 'G9' of office is not in load_profiles"),
    (("classes", 0, "electricity_kwh_per_m2_net"), -5, "electricity_kwh_per_m2_net of office"),
    (("classes", 0, "source"), "", "source of office must be a text"),
    (("classes", 0, "consumer_category"), "Residential", "consumer_category of office"),
    (("classes", 1, "large", "from_m2_net"), None, "large.from_m2_net of retail"),
    (("classes", 1, "large", "source"), KeyError, "large.source of retail"),
    (("classes", 1, "load_class"), "office", "classes listed twice: ['office']"),
    (("classes",), [], "classes must be a list"),
])
def test_an_invalid_class_table_is_refused_with_the_value_named(path, value, message):
    with pytest.raises(ValueError, match=message.replace("(", r"\(").replace("[", r"\[")):
        table(changed(path, value))


def test_the_sql_parameters_hold_null_for_missing_values():
    params = load_classes.sql_parameters(table())
    assert params["classes"][0] == "office" and params["large_from"][0] is None
    assert params["categories"][-1] is None and params["large_from"][1] == pytest.approx(300 / 0.85)
    assert len({len(v) for v in params.values()}) == 1


def test_the_class_of_unclassified_parts_must_be_configured():
    assert load_classes.unclassified_load_class(None, None) is None
    assert load_classes.unclassified_load_class("other", table()) == "other"
    for value, t in (("spaceport", table()), ("other", None)):
        with pytest.raises(ValueError, match="UNCLASSIFIED_LOAD_CLASS must be null or a class of LOAD_CLASSES"):
            load_classes.unclassified_load_class(value, t)


# --- connection levels --------------------------------------------------------------------------------------------
THRESHOLDS = {"station_busbar": 100.0, "mv": 200.0, "hv": 20000.0}


@pytest.mark.parametrize("nonres, mv_direct, lv, level", [
    (0, False, 100.0, "lv_feeder"),
    (0, False, 100.1, "station_busbar"),
    (150, False, 180.0, "station_busbar"),
    (200.1, True, 20.0, "mv"),
    (19999.9, True, 0.0, "mv"),
    (20000, True, 0.0, "hv"),
])
def test_the_connection_level_follows_the_own_peak(nonres, mv_direct, lv, level):
    assert load_classes.connection_level(nonres, mv_direct, lv, THRESHOLDS) == level


def test_the_connection_thresholds_must_rise_and_none_switches_them_off():
    assert load_classes.connection_thresholds(None, 200) is None
    assert load_classes.connection_thresholds({"station_busbar": 100, "hv": 20000}, 200) == THRESHOLDS
    for bad in ({"station_busbar": 300, "hv": 20000}, {"station_busbar": 100, "hv": 150},
                {"station_busbar": 100}, {"station_busbar": "100", "hv": 20000}):
        with pytest.raises(ValueError, match="CONNECTION_LEVEL_THRESHOLDS_KW"):
            load_classes.connection_thresholds(bad, 200)


# --- the generation SQL -------------------------------------------------------------------------------------------
class Cursor:
    """Records the statements; a count query of the class step gets two zeros, the others three."""

    def __init__(self):
        self.log, self.rowcount = [], 3

    def execute(self, query, params=None):
        self.log.append((" ".join(str(query).split()), params))

    def fetchone(self):
        return (0, 0) if "FROM buildings_tem b WHERE b.load_class IS NOT NULL" in self.log[-1][0] else (0, 0, 0)

    def fetchall(self):
        return [("lv_feeder", 5), ("mv", 1)]


def mixin(cursor=None):
    m = object.__new__(PreprocessingMixin)
    m.cur, m.logger = cursor or Cursor(), logging.getLogger("load-class-test")
    return m


#: sha256 of the statements (whitespace normalised) that set_building_peak_load and
#: update_too_large_consumers_to_zero executed at 2dfba770, before the load classes
OLD_LOAD_SQL = "ce0ea1e76ae366abbe1d8e76f9bb51ae330f8b52330fab3f9f51d3485addb457"


def test_the_model_building_use_runs_the_load_sql_of_the_versions_before_the_classes():
    m = mixin()
    m.set_building_peak_load()
    m.update_too_large_consumers_to_zero()
    assert hashlib.sha256("\n".join(q for q, _ in m.cur.log).encode()).hexdigest() == OLD_LOAD_SQL
    executed = len(m.cur.log)
    assert m.set_connection_levels() == {} and len(m.cur.log) == executed  # no levels without thresholds


def test_the_model_load_class_gives_classified_commercial_and_public_parts_their_class_load(monkeypatch):
    t = table()
    monkeypatch.setattr(preprocessing_mixin, "NONRESIDENTIAL_LOAD_MODEL", "load_class")
    monkeypatch.setattr(preprocessing_mixin, "LOAD_CLASS_TABLE", t)
    m = mixin()
    m.set_building_peak_load()
    statements = [q for q, _ in m.cur.log]
    update = next(i for i, q in enumerate(statements) if "WITH classes AS" in q)
    query, params = m.cur.log[update]
    assert "AND b.nonresidential_use = ANY(%(uses)s)" in query and params["uses"] == ["Commercial", "Public"]
    assert "CASE WHEN b.nonresidential_floor_area > c.large_from THEN c.large_coincident" in query
    assert "ELSE c.coincident END / s.sim_factor / 1000" in query
    assert "s.category = COALESCE(c.consumer_category, b.nonresidential_use)" in query
    assert params["classes"] == list(t["load_class"])
    assert dict(zip(params["sim_categories"], params["sim_factors"])) == {
        c: config_loader.SIM_FACTOR[c] for c in ("Commercial", "Public")}
    # after the loads per use and before the checks; the building's peak is summed again
    assert "SET residential_peak_load_in_kw" in statements[update - 2]
    assert statements[update + 1].startswith("UPDATE buildings_tem SET peak_load_in_kw =")
    # without UNCLASSIFIED_LOAD_CLASS a part without a class matches no class and keeps the W/m² of its use
    assert "WHERE c.load_class = COALESCE(b.load_class, %(unclassified)s::text)" in query
    assert params["unclassified"] is None and not any("WHERE b.load_class IS NULL" in q for q in statements)


def test_parts_without_a_class_take_the_unclassified_load_class(monkeypatch):
    monkeypatch.setattr(preprocessing_mixin, "NONRESIDENTIAL_LOAD_MODEL", "load_class")
    monkeypatch.setattr(preprocessing_mixin, "LOAD_CLASS_TABLE", table())
    monkeypatch.setattr(preprocessing_mixin, "UNCLASSIFIED_LOAD_CLASS", "other")
    m = mixin()
    assert m.apply_load_classes() == {"classified": 3, "unclassified": 0, "unconfigured": 0, "not_classified_use": 0}
    statements = [q for q, _ in m.cur.log]
    count = next(q for q in statements if "WHERE b.load_class IS NULL" in q)
    assert "b.nonresidential_use = ANY(%(uses)s)" in count
    query, params = next((q, p) for q, p in m.cur.log if "WITH classes AS" in q)
    assert params["unclassified"] == "other"


def test_the_connection_levels_use_the_households_law_and_the_thresholds(monkeypatch):
    monkeypatch.setattr(preprocessing_mixin, "CONNECTION_LEVEL_THRESHOLDS_KW", THRESHOLDS)
    m = mixin()
    assert m.set_connection_levels() == {"lv_feeder": 5, "mv": 1}
    (query, params), = m.cur.log
    assert "WHEN nonresidential_mv_direct AND nonresidential_peak_load_in_kw >= %(hv)s THEN 'hv'" in query
    assert "power(households::double precision, -0.75)" in query
    assert params["g_res"] == config_loader.SIM_FACTOR["Residential"]
    assert (params["hv"], params["busbar"]) == (20000.0, 100.0)


# --- the version snapshot -----------------------------------------------------------------------------------------
def snapshot_of(monkeypatch, model="building_use", levels=None):
    monkeypatch.setattr(preprocessing_mixin, "NONRESIDENTIAL_LOAD_MODEL", model)
    monkeypatch.setattr(preprocessing_mixin, "LOAD_CLASS_TABLE", table() if model == "load_class" else None)
    monkeypatch.setattr(preprocessing_mixin, "LOAD_CLASSES", shipped())
    monkeypatch.setattr(preprocessing_mixin, "CONNECTION_LEVEL_THRESHOLDS_KW", levels)
    return json.loads(json.dumps(PreprocessingMixin._generation_parameters_snapshot(object.__new__(PreprocessingMixin)),
                                 allow_nan=False))


def before_the_classes(snapshot):
    old = copy.deepcopy(snapshot)
    old["load_calculation"].pop("nonresidential_load_model")
    old.pop("connection_levels")
    return old


def test_a_version_stored_before_the_classes_continues_with_the_model_building_use(monkeypatch):
    current = snapshot_of(monkeypatch)
    assert "load_classes" not in current["load_calculation"] and current["connection_levels"] is None
    assert compare_snapshots(before_the_classes(current), current) == ([], [])


def test_the_class_model_or_connection_levels_need_a_new_version(monkeypatch):
    old = before_the_classes(snapshot_of(monkeypatch))
    classes = snapshot_of(monkeypatch, model="load_class")
    differences = compare_snapshots(old, classes)[0]
    assert "load_calculation.nonresidential_load_model" in differences
    assert all(d.startswith("load_calculation.") for d in differences)
    levels = snapshot_of(monkeypatch, levels=THRESHOLDS)
    assert compare_snapshots(old, levels)[0] == ["connection_levels"]
    # a version of the class model records the inputs, the derived values and their sources
    recorded = classes["load_calculation"]["load_classes"]
    office = recorded["classes"][0]
    assert recorded["net_floor_area_factor"] == 0.85 and recorded["load_profiles"]["G1"] == 2084
    assert office["peak_load_per_m2"] == {"Commercial": pytest.approx(28.55, abs=0.01)}
    assert "BMWi/BMUB 2015" in office["source"]
    assert set(recorded["classes"][-1]["peak_load_per_m2"]) == {"Commercial", "Public"}      # other: by use
    assert classes["load_calculation"]["unclassified_load_class"] is None
    changed_value = copy.deepcopy(classes)
    changed_value["load_calculation"]["load_classes"]["classes"][0]["electricity_kwh_per_m2_net"] = 40
    assert compare_snapshots(classes, changed_value)[0] == [
        "load_calculation.load_classes.classes"]


def test_a_class_version_stored_before_the_unclassified_class_kept_the_use_load(monkeypatch):
    stored = snapshot_of(monkeypatch, model="load_class")
    stored["load_calculation"].pop("unclassified_load_class")
    assert compare_snapshots(stored, snapshot_of(monkeypatch, model="load_class")) == ([], [])
    monkeypatch.setattr(preprocessing_mixin, "UNCLASSIFIED_LOAD_CLASS", "other")
    current = json.loads(json.dumps(PreprocessingMixin._generation_parameters_snapshot(
        object.__new__(PreprocessingMixin)), allow_nan=False))
    assert current["load_calculation"]["unclassified_load_class"] == "other"
    assert compare_snapshots(stored, current)[0] == ["load_calculation.unclassified_load_class"]
    # the model building_use records no class for the unclassified parts, whatever the key says
    assert snapshot_of(monkeypatch)["load_calculation"]["unclassified_load_class"] is None
