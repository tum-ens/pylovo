"""HV/MV substation rules of the MV stage and the SQL wrappers of MvSourceMixin (no database).

Pins the OSM filter rules, the 75 m merge, the EHV rule, the 150 m duplicate rule, the voltage and rating parsing,
the eGon cross-check and the tables handed to the stage; the mixin is run against a fake cursor.
"""

import logging
import random

import numpy as np
import pandas as pd
import pytest
import shapely
from psycopg2 import sql

import pylovo.database.mv_source_mixin as source_mixin
from pylovo.database.mv_source_mixin import MvSourceMixin
from pylovo.mv_grid import substations as subs
from pylovo.mv_grid.parameters import MvParameters, MvStageError
from pylovo.mv_grid.substations import (
    CANDIDATE_COLUMNS,
    SITE_COLUMNS,
    Site,
    aggregate,
    classify_candidate,
    filter_candidates,
    match_egon,
    parse_voltages,
    select_osm_sites,
    sites_frame,
)

MERGE_M, DROP_M, EHV_M = 75.0, 150.0, 1000.0


def _square(x: float, y: float, half: float) -> bytes:
    return shapely.to_wkb(shapely.MultiPolygon([shapely.box(x - half, y - half, x + half, y + half)]))


def cand(source_id: str, x: float = 0.0, y: float = 0.0, half: float | None = None,
         voltage: str | None = "110000;20000", **tags) -> dict:
    """A candidate row: a square footprint of half side ``half`` around (x, y), or a node without footprint."""
    row = dict.fromkeys(CANDIDATE_COLUMNS)
    row.update(source_id=source_id, voltage=voltage, x=float(x), y=float(y),
               area_m2=(2.0 * half) ** 2 if half else 0.0,
               footprint_wkb=_square(x, y, half) if half else None, in_plant=False)
    row.update(tags)
    return row


def frame(*rows: dict) -> pd.DataFrame:
    return pd.DataFrame(list(rows), columns=list(CANDIDATE_COLUMNS))


# ---------------------------------------------------------------------------------------------- parsing
@pytest.mark.parametrize("text, expected", [
    ("110000;20000", {110000, 20000}),
    ("380000;220000;110000", {380000, 220000, 110000}),
    ("110000", {110000}),
    ("110 kV", {110000}),
    ("110kV;20kV", {110000, 20000}),
    ("20_kv", {20000}),
    ("110", {110000}),                       # OSM shorthand in kV
    ("110;20", {110000, 20000}),
    ("0.4", {400}),
    ("20000;400", {20000, 400}),             # LV value in volts next to a volts value, not 400 kV
    ("20000/400", {20000, 400}),
    ("20", {20000}),
    ("20000;0.4", {20000, 400}),
    ("110000;medium", {110000}),             # words are skipped
    ("10,5 kV", {10500}),                    # decimal comma
    ("110000,20000", {110000, 20000}),       # comma between values
    ("110/20 kV", {110000, 20000}),
    (" 110000 ; 20000 ", {110000, 20000}),
    ("unknown", set()),
    ("", set()),
    (None, set()),
    (float("nan"), set()),
])
def test_voltage_tags_are_read_in_volts(text, expected):
    assert parse_voltages(text) == expected


# ---------------------------------------------------------------------------------------------- filter rules
@pytest.mark.parametrize("changes, expected", [
    ({}, (True, "kept")),
    ({"substation_tag": "transmission"}, (True, "kept")),          # many DSO substations are tagged so
    ({"substation_tag": "distribution"}, (True, "kept")),
    ({"voltage": "60000;20000"}, (True, "kept")),
    ({"voltage": "20000"}, (False, "no_hv")),
    ({"voltage": "380000;220000"}, (False, "no_hv")),
    ({"voltage": None}, (False, "no_hv")),
    ({"voltage": "110000;15000", "substation_tag": "traction", "frequency": "16.7"}, (False, "tag_traction")),
    ({"substation_tag": "Transition"}, (False, "tag_transition")),
    ({"substation_tag": "converter"}, (False, "tag_converter")),
    ({"substation_tag": "generation"}, (False, "tag_generation")),
    ({"substation_tag": "industrial"}, (False, "tag_industrial")),
    ({"substation_tag": "minor_distribution"}, (False, "tag_minor_distribution")),
    ({"substation_tag": "transmission", "frequency": "16.7"}, (False, "frequency")),
    ({"frequency": "50;16.7"}, (False, "frequency")),
    ({"frequency": "16,7"}, (False, "frequency")),
    ({"frequency": "16.67"}, (False, "frequency")),
    ({"frequency": "16 2/3"}, (False, "frequency")),
    ({"frequency": "0"}, (False, "frequency")),                     # DC
    ({"frequency": "50"}, (True, "kept")),
    ({"operator": "DB Energie GmbH"}, (False, "railway_operator")),
    ({"operator": "Deutsche Bahn AG"}, (False, "railway_operator")),
    ({"name": "Unterwerk Nord"}, (False, "railway_name")),
    ({"name": "Umformerwerk"}, (False, "railway_name")),
    ({"railway": "substation"}, (False, "railway_tag")),
    ({"railway": "no"}, (True, "kept")),
    ({"in_plant": True}, (False, "in_plant")),
    ({"in_plant": np.True_}, (False, "in_plant")),
    ({"in_plant": True, "plant": "solar:solarpark nord"}, (False, "in_plant")),
    ({"in_plant": True, "plant": "hydro:wasserkraftwerk"}, (False, "in_plant")),
    ({"in_plant": True, "plant": "gas:heizkraftwerk nord"}, (True, "kept")),          # CHP site of a city utility
    ({"in_plant": True, "plant": ":heizwerk süd|:"}, (True, "kept")),
    ({"voltage": None, "substation_tag": "transmission"}, (True, "kept")),          # city utility, voltage not tagged
    ({"voltage": None, "substation_tag": "transmission", "area_m2": 0.0}, (False, "no_hv")),    # a point needs it
    ({"voltage": "", "substation_tag": "transmission"}, (True, "kept")),
    ({"voltage": None, "substation_tag": "distribution"}, (False, "no_hv")),
    ({"voltage": "medium", "substation_tag": "transmission"}, (False, "no_hv")),
    ({"voltage": "220000", "substation_tag": "transmission"}, (False, "no_hv")),
    ({"operator": "Stadtwerke", "name": "Umspannwerk Süd"}, (True, "kept")),
])
def test_filter_rules_keep_hv_mv_substations_only(changes, expected):
    row = cand("way/1", half=20)
    row.update(changes)
    assert classify_candidate(row) == expected


def test_filter_reads_pandas_rows_with_missing_tags():
    rows = frame(cand("way/1", half=20), cand("node/2", voltage=None),
                 cand("way/3", half=20, frequency="16.7", operator=np.nan))
    decisions = filter_candidates(rows)
    assert decisions["keep"].tolist() == [True, False, False]
    assert decisions["reason"].tolist() == ["kept", "no_hv", "frequency"]
    assert decisions["keep"].dtype == bool
    assert classify_candidate(rows.iloc[0]) == (True, "kept")       # a pandas Series works too
    assert set(subs.CANDIDATE_REASONS) >= set(decisions["reason"])


# ---------------------------------------------------------------------------------------------- aggregation
def test_candidates_within_75_m_merge_into_one_site():
    rows = frame(cand("way/10", 0, 0, half=30, name="Big", operator="Netz A"),
                 cand("node/5", 10, 10, name="Node name", voltage="110000"),          # inside the footprint
                 cand("way/11", 100, 0, half=10, voltage="110000;20000"))           # 60 m gap
    sites = aggregate(rows, MERGE_M, DROP_M, EHV_M)
    assert len(sites) == 1
    site = sites[0]
    assert site.source_ids == ["node/5", "way/10", "way/11"]
    assert site.area_m2 == pytest.approx(60.0 ** 2 + 20.0 ** 2)
    assert shapely.from_wkb(site.footprint_wkb).geom_type == "MultiPolygon"
    assert shapely.box(-30, -30, 30, 30).contains(site.point())     # busbar in the largest polygon
    assert site.name == "Big" and site.operator == "Netz A"
    assert site.voltage_tag == "110000|110000;20000"
    assert site.max_v == 110000 and site.mv_tagged and site.flags == []
    assert site.key == "node/5"


def test_merging_is_transitive():
    rows = frame(cand("node/1", 0, 0), cand("node/2", 70, 0), cand("node/3", 140, 0))
    sites = aggregate(rows, MERGE_M, DROP_M, EHV_M)
    assert [s.source_ids for s in sites] == [["node/1", "node/2", "node/3"]]
    assert (sites[0].x, sites[0].y) == pytest.approx((70.0, 0.0))  # points only: their mean
    assert sites[0].footprint_wkb is None and sites[0].area_m2 == 0.0


def test_of_two_sites_closer_than_150_m_the_smaller_footprint_is_dropped():
    dropped = []
    rows = frame(cand("way/2", 0, 0, half=50), cand("way/1", 170, 0, half=10))   # gap 110 m
    sites = aggregate(rows, MERGE_M, DROP_M, EHV_M, dropped=dropped)
    assert [s.key for s in sites] == ["way/2"]
    assert [(site.key, reason, other) for site, reason, other in dropped] == [("way/1", "near_larger", "way/2")]

    far = frame(cand("way/2", 0, 0, half=50), cand("way/1", 250, 0, half=10))    # gap 190 m
    assert [s.key for s in aggregate(far, MERGE_M, DROP_M, EHV_M)] == ["way/1", "way/2"]


def test_equal_areas_keep_the_smaller_source_id():
    dropped = []
    sites = aggregate(frame(cand("node/2", 100, 0), cand("node/1", 0, 0)), MERGE_M, DROP_M, EHV_M, dropped=dropped)
    assert [s.key for s in sites] == ["node/1"]
    assert dropped[0][0].key == "node/2"


def test_ehv_site_without_mv_level_next_to_an_hv_mv_site_is_dropped():
    dropped = []
    rows = frame(cand("way/1", 0, 0, half=100, voltage="380000;110000", substation_tag="transmission"),
                 cand("way/2", 600, 0, half=20, voltage="110000;20000"))          # gap 480 m
    sites = aggregate(rows, MERGE_M, DROP_M, EHV_M, dropped=dropped)
    assert [s.key for s in sites] == ["way/2"]
    assert [(site.key, reason, other) for site, reason, other in dropped] == [("way/1", "ehv_partner", "way/2")]


def test_ehv_site_without_mv_level_alone_is_kept_and_flagged():
    rows = frame(cand("way/1", 0, 0, half=100, voltage="380000;110000"),
                 cand("way/2", 3000, 0, half=20, voltage="110000;20000"))
    sites = aggregate(rows, MERGE_M, DROP_M, EHV_M)
    assert {s.key: s.flags for s in sites} == {"way/1": ["ehv_uncertain"], "way/2": []}
    assert sites[0].max_v == 380000 and not sites[0].mv_tagged


def test_ehv_site_with_mv_level_is_kept_without_flag():
    rows = frame(cand("way/1", 0, 0, half=100, voltage="380000;110000;20000"),
                 cand("way/2", 600, 0, half=20, voltage="110000;20000"))
    sites = aggregate(rows, MERGE_M, DROP_M, EHV_M)
    assert {s.key: s.flags for s in sites} == {"way/1": [], "way/2": []}


def test_ehv_and_hv_mv_footprints_within_75_m_are_one_site():
    rows = frame(cand("way/1", 0, 0, half=100, voltage="380000;110000"),
                 cand("way/2", 150, 0, half=20, voltage="110000;20000"))          # gap 30 m
    sites = aggregate(rows, MERGE_M, DROP_M, EHV_M)
    assert len(sites) == 1
    assert sites[0].source_ids == ["way/1", "way/2"]
    assert sites[0].max_v == 380000 and sites[0].mv_tagged and sites[0].flags == []
    assert sites[0].voltage_tag == "110000;20000|380000;110000"


def test_aggregation_does_not_depend_on_the_row_order():
    rows = [cand(f"way/{i}", 37.0 * i * i, 11.0 * i, half=5 + i) for i in range(12)]
    reference = [(s.source_ids, s.x, s.y, s.area_m2) for s in aggregate(frame(*rows), MERGE_M, DROP_M, EHV_M)]
    random.Random(4).shuffle(rows)
    assert [(s.source_ids, s.x, s.y, s.area_m2) for s in aggregate(frame(*rows), MERGE_M, DROP_M, EHV_M)] == reference
    assert aggregate(frame(), MERGE_M, DROP_M, EHV_M) == []


def test_invalid_or_missing_footprints_fall_back_to_the_point():
    bowtie = shapely.to_wkb(shapely.Polygon([(0, 0), (40, 40), (40, 0), (0, 40), (0, 0)]))
    rows = frame(cand("way/1", 20, 20, footprint_wkb=bowtie, area_m2=0.0),
                 cand("way/2", 500, 0, footprint_wkb=memoryview(_square(500, 0, 10))),
                 cand("way/3", 900, 0, footprint_wkb=b"not wkb"))
    sites = aggregate(rows, MERGE_M, DROP_M, EHV_M)
    assert [s.key for s in sites] == ["way/1", "way/2", "way/3"]
    assert sites[0].area_m2 == pytest.approx(800.0)                 # made valid: two triangles
    assert sites[1].area_m2 == pytest.approx(400.0)
    assert sites[2].footprint_wkb is None and (sites[2].x, sites[2].y) == (900.0, 0.0)


# ---------------------------------------------------------------------------------------------- selection
def test_select_osm_sites_filters_merges_and_reports():
    candidates = frame(
        cand("way/1", 0, 0, half=40, substation_tag="transmission"),
        cand("node/2", 5, 5, voltage="110000"),
        cand("way/3", 5000, 0, half=30, substation_tag="traction", voltage="110000;15000", frequency="16.7"),
        cand("way/4", 9000, 0, half=30, in_plant=True),
        cand("way/5", 12000, 0, half=5, voltage="20000;400"),
        cand("way/6", 20000, 0, half=200, voltage="380000;110000"),
        cand("way/7", 20500, 0, half=20, voltage="110000;20000"),
    )
    selection = select_osm_sites(candidates, MvParameters())
    assert [s.source_ids for s in selection.sites] == [["node/2", "way/1"], ["way/7"]]
    assert dict(zip(selection.decisions["source_id"], selection.decisions["reason"])) == {
        "way/1": "kept", "node/2": "kept", "way/3": "tag_traction", "way/4": "in_plant", "way/5": "no_hv",
        "way/6": "kept", "way/7": "kept"}
    assert [(site.key, reason) for site, reason, _ in selection.dropped] == [("way/6", "ehv_partner")]
    assert selection.reason_counts() == {"ehv_partner": 1, "in_plant": 1, "no_hv": 1, "tag_traction": 1}
    line = selection.describe()
    assert "7 candidates, 4 kept, 2 sites (1 candidates merged, 0 ehv_uncertain)" in line
    assert "tag_traction 1" in line


def test_select_osm_sites_fails_clearly_without_hv_mv_substations():
    params = MvParameters()
    with pytest.raises(MvStageError, match=r"no HV/MV substations within 5 km .*INFDB_OSM_SCHEMA"):
        select_osm_sites(frame(), params)
    with pytest.raises(MvStageError, match=r"none of the 1 OSM substations within 5 km .*no_hv 1"):
        select_osm_sites(frame(cand("way/1", voltage="20000")), params)
    with pytest.raises(MvStageError, match=r"within 2 km"):
        select_osm_sites(frame(), params.replace(region_buffer_m=2000.0))


# ---------------------------------------------------------------------------------------------- eGon
def _sites() -> list[Site]:
    return aggregate(frame(cand("way/1", 0, 0, half=50), cand("node/2", 1000, 0), cand("node/3", 5000, 0),
                           cand("node/4", 9000, 0)), MERGE_M, DROP_M, EHV_M)


EGON = pd.DataFrame({"bus_id": [7, 8, 9, 11, 10], "voltage": ["110000;20000", None, "110000", None, None],
                     "osm_id": ["w1", None, None, None, None],
                     "x": [10.0, 1200.0, 5400.0, 9100.0, 8900.0], "y": [10.0, 0.0, 0.0, 0.0, 0.0]})


def test_each_site_gets_the_nearest_egon_substation_within_300_m():
    assert match_egon(_sites(), EGON, 300.0) == [(8, 200.0), (None, None), (10, 100.0), (7, 0.0)]
    assert match_egon(_sites(), EGON.iloc[0:0], 300.0) == [(None, None)] * 4
    assert match_egon([], EGON, 300.0) == []


def test_sites_frame_has_the_substation_columns():
    sites = _sites()
    region = shapely.to_wkb(shapely.box(-100, -100, 2000, 100))
    table = sites_frame(sites, region, match_egon(sites, EGON, 300.0))
    assert tuple(table.columns) == SITE_COLUMNS
    assert table["source_ids"].tolist() == [["node/2"], ["node/3"], ["node/4"], ["way/1"]]
    assert table["in_region"].tolist() == [True, False, False, True]
    assert table["egon_bus_id"].tolist() == [8, pd.NA, 10, 7]
    assert table["egon_distance_m"].isna().tolist() == [False, True, False, False]
    # a site without an eGon match has no eTraGo 110 kV bus either
    assert ["no_egon_bus" in f for f in table["flags"]] == [False, True, False, False]
    assert not any("no_egon_bus" in f for f in sites_frame(sites)["flags"])           # no cross-check: no flag
    assert table["osm_ratings_mva"].tolist() == [None] * 4                         # no OSM ratings are read
    assert table["source"].unique().tolist() == ["osm"]
    assert table["footprint_wkb"].iloc[3] is not None and table["footprint_wkb"].iloc[0] is None
    assert sites_frame(sites)["in_region"].tolist() == [None] * 4
    assert list(sites_frame([]).columns) == list(SITE_COLUMNS)
    with pytest.raises(ValueError, match="matches"):
        sites_frame(sites, region, [(None, None)])


# ---------------------------------------------------------------------------------------------- mixin SQL
def _render(query) -> str:
    """Text of a psycopg2 sql object without a connection (identifiers double-quoted)."""
    if isinstance(query, str):
        return query
    if isinstance(query, sql.Composed):
        return "".join(_render(part) for part in query.seq)
    if isinstance(query, sql.SQL):
        return query.string
    if isinstance(query, sql.Identifier):
        return ".".join('"' + s + '"' for s in query.strings)
    if isinstance(query, sql.Literal):
        return repr(query.wrapped)
    raise TypeError(type(query))


class FakeCursor:
    """Records the rendered queries; ``respond(text, params)`` returns the rows of each query."""

    def __init__(self, respond):
        self.respond = respond
        self.calls: list[tuple[str, object]] = []
        self.rows: list = []

    def execute(self, query, params=None):
        text = " ".join(_render(query).split())
        self.calls.append((text, params))
        self.rows = list(self.respond(text, params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


def _client(respond) -> MvSourceMixin:
    client = object.__new__(MvSourceMixin)
    client.cur = FakeCursor(respond)
    client.logger = logging.getLogger("mv-source-test")
    return client


@pytest.fixture
def schemas(monkeypatch):
    monkeypatch.setattr(source_mixin, "INFDB_OSM_SCHEMA", "osm_x")
    monkeypatch.setattr(source_mixin, "INFDB_OPENDATA_SCHEMA", "open_x")
    monkeypatch.setattr(source_mixin, "TARGET_EPSG", 25832)


def test_osm_candidates_query_the_osm_schema_and_return_bytes(schemas):
    footprint = _square(0, 0, 10)
    row = ("way/1", "110000;20000", None, "50", "Netz", "UW", None, 1.0, 2.0, 400.0, memoryview(footprint), True,
           "solar:pv")
    client = _client(lambda text, params: [(4326,)] if text.startswith("SELECT ST_SRID") else [row])
    region = shapely.to_wkb(shapely.box(0, 0, 10, 10))
    result = client.osm_substation_candidates(memoryview(region), 5000)
    assert [t for t, _ in client.cur.calls[:2]] == [f'SELECT ST_SRID(geom) FROM "osm_x"."{table}" WHERE geom IS NOT '
                                                    "NULL LIMIT 1" for table in ("infrastructure_point",
                                                                                 "infrastructure_polygon")]
    text, params = client.cur.calls[-1]
    for needle in ('"osm_x".infrastructure_point', '"osm_x".infrastructure_polygon', '"osm_x".tags',
                   "t.geom_type = c.gt AND t.osm_id = c.tag_id", "osm_subtype IN ('plant', 'generator')",
                   "ST_DWithin(c.pt, ST_GeomFromWKB(%(region)s, 25832), %(buffer_m)s)", "'relation/' ||",
                   "p.geom && ST_Transform(ST_GeomFromWKB(%(box)s, 25832), %(point_srid)s)",
                   "g.geom && ST_Transform(ST_GeomFromWKB(%(box)s, 25832), %(polygon_srid)s)"):
        assert needle in text
    assert params["region"] == region and params["buffer_m"] == 5000.0 and isinstance(params["region"], bytes)
    assert params["point_srid"] == params["polygon_srid"] == 4326
    # the prefilter box holds the region widened by the buffer and the margin, with a vertex every 1 km at most
    box = shapely.from_wkb(params["box"])
    assert box.bounds == (-6000.0, -6000.0, 6010.0, 6010.0)
    assert shapely.get_num_coordinates(box) > 4 * 12
    assert tuple(result.columns) == CANDIDATE_COLUMNS == source_mixin.OSM_CANDIDATE_COLUMNS
    assert result["footprint_wkb"].iloc[0] == footprint and isinstance(result["footprint_wkb"].iloc[0], bytes)
    assert result["in_plant"].dtype == bool and result["x"].dtype == float
    assert classify_candidate(result.iloc[0]) == (False, "in_plant")

    empty = _client(lambda text, params: []).osm_substation_candidates(region, 5000)
    assert tuple(empty.columns) == CANDIDATE_COLUMNS and len(empty) == 0


def test_osm_objects_and_landuse_are_prefiltered_in_the_srid_of_each_layer(schemas):
    srids = {"infrastructure_point": 4326, "infrastructure_polygon": 3857, "building_polygon": 4258,
             "landuse_polygon": 31468}

    def respond(text, params):
        table = next((t for t in srids if f'"osm_x"."{t}"' in text), None)
        return [(srids[table],)] if text.startswith("SELECT ST_SRID") else []

    region = shapely.to_wkb(shapely.box(0, 0, 10, 10))
    client = _client(respond)
    client.osm_power_objects(region)
    text, params = client.cur.calls[-1]
    for alias, key in (("p", "point_srid"), ("g", "polygon_srid"), ("b", "building_srid")):
        assert f"{alias}.geom && ST_Transform(ST_GeomFromWKB(%(box)s, 25832), %({key})s)" in text
    assert (params["point_srid"], params["polygon_srid"], params["building_srid"]) == (4326, 3857, 4258)
    assert shapely.from_wkb(params["box"]).bounds == (-2000.0, -2000.0, 2010.0, 2010.0)    # 1 km reach + margin
    client = _client(respond)
    client.osm_landuse(region)
    text, params = client.cur.calls[-1]
    assert "l.geom && ST_Transform(ST_GeomFromWKB(%(box)s, 25832), %(landuse_srid)s)" in text
    assert params["landuse_srid"] == 31468 and shapely.from_wkb(params["box"]).bounds == (-1000.0, -1000.0, 1010.0,
                                                                                          1010.0)


def test_osm_queries_need_the_osm_schema(schemas, monkeypatch):
    monkeypatch.setattr(source_mixin, "INFDB_OSM_SCHEMA", None)
    client = _client(lambda text, params: [])
    with pytest.raises(MvStageError, match="INFDB_OSM_SCHEMA"):
        client.osm_substation_candidates(b"x", 5000)
    assert client.cur.calls == []


def _egon_responder(present: dict[str, bool], rows: list | None = None):
    """Fake catalogue: ``present[schema.table]`` = has rows (missing key = table does not exist)."""
    def respond(text, params):
        if text.startswith("SELECT c IS NOT NULL"):
            name = params[0].replace('"', "")
            return [(name in present,)]
        if text.startswith("SELECT EXISTS"):
            name = text.split("FROM ")[1].rstrip(")").replace('"', "")
            return [(present[name],)]
        return rows or []
    return respond


def test_egon_table_prefers_the_filled_pylovo_table_then_opendata(schemas):
    both = {"pylovo.egon_hvmv_substation": True, "open_x.egon_hvmv_substation": True}
    assert _client(_egon_responder(both)).egon_available() == (True, "pylovo")
    empty_pylovo = {**both, "pylovo.egon_hvmv_substation": False}
    assert _client(_egon_responder(empty_pylovo)).egon_available() == (True, "open_x")
    ok, reason = _client(_egon_responder({})).egon_available()
    assert not ok and "egon_hvmv_substation" in reason and "populate the eGon-data table" in reason


def test_egon_readers_map_na_to_none_and_fail_without_tables(schemas):
    present = {"open_x.egon_hvmv_substation": True}
    rows = [(501, "NA", "w123", 10.0, 20.0), (502, "110000;20000", "NA", 30.0, 40.0)]
    client = _client(_egon_responder(present, rows))
    result = client.egon_substations(b"region", 5000)
    text, params = client.cur.calls[-1]
    assert '"open_x"."egon_hvmv_substation"' in text and "NULLIF(btrim(s.voltage::text), 'NA')" in text
    assert params == {"region": b"region", "buffer_m": 5000.0}
    assert tuple(result.columns) == source_mixin.EGON_SUBSTATION_COLUMNS
    assert result["voltage"].tolist() == [None, "110000;20000"]
    assert result["osm_id"].tolist() == ["w123", None]
    assert result["bus_id"].dtype == np.int64

    missing = _client(_egon_responder({}))
    with pytest.raises(MvStageError, match="no eGon-data table egon_hvmv_substation"):
        missing.egon_substations(b"region", 5000)
