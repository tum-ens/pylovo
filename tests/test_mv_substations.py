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
    egon_sites,
    filter_candidates,
    known_units_mva,
    match_egon,
    parse_rating_mva,
    parse_voltages,
    select_osm_sites,
    site_ratings,
    sites_frame,
    unmatched_egon,
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


@pytest.mark.parametrize("text, expected", [
    ("40 MVA", 40.0),
    ("40MVA", 40.0),
    ("31,5 MVA", 31.5),
    ("31.5 MVA;40 MVA", 40.0),               # cooling stages: the largest
    ("40000 kVA", 40.0),
    ("63000000 VA", 63.0),
    ("80", 80.0),                            # bare number below 1000: MVA
    ("40000", 40.0),                         # bare number below 1e6: kVA
    ("25 MW", 25.0),
    ("abc", None),
    ("0 MVA", None),
    (None, None),
])
def test_transformer_ratings_are_read_in_mva(text, expected):
    result = parse_rating_mva(text)
    assert result == (pytest.approx(expected) if expected is not None else None)


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
    assert site.source == "osm" and site.key == "node/5"


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
    with pytest.raises(MvStageError, match=r"no HV/MV substations within 5 km .*MV_SUBSTATIONS=egon"):
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
    assert unmatched_egon(_sites(), EGON, 300.0) == [9]
    assert unmatched_egon([], EGON, 300.0) == [7, 8, 9, 10, 11]


def test_egon_substations_become_sites():
    sites = egon_sites(EGON)
    assert [s.source_ids for s in sites] == [["egon/7"], ["egon/8"], ["egon/9"], ["egon/10"], ["egon/11"]]
    first = sites[0]
    assert (first.x, first.y, first.area_m2, first.footprint_wkb) == (10.0, 10.0, 0.0, None)
    assert first.max_v == 110000 and first.mv_tagged and first.voltage_tag == "110000;20000"
    assert sites[1].max_v == 110000 and not sites[1].mv_tagged and sites[1].voltage_tag == ""
    assert all(site.source == "egon" for site in sites)
    assert match_egon(sites, EGON, 300.0) == [(7, 0.0), (8, 0.0), (9, 0.0), (10, 0.0), (11, 0.0)]
    assert egon_sites(EGON.iloc[0:0]) == []


def test_sites_frame_has_the_substation_columns():
    sites = _sites()
    region = shapely.to_wkb(shapely.box(-100, -100, 2000, 100))
    table = sites_frame(sites, region, match_egon(sites, EGON, 300.0), [(), (), (), (40.0, 40.0)])
    assert tuple(table.columns) == SITE_COLUMNS
    assert table["source_ids"].tolist() == [["node/2"], ["node/3"], ["node/4"], ["way/1"]]
    assert table["in_region"].tolist() == [True, False, False, True]
    assert table["egon_bus_id"].tolist() == [8, pd.NA, 10, 7]
    assert table["egon_distance_m"].isna().tolist() == [False, True, False, False]
    # a site without an eGon match has no eTraGo 110 kV bus either
    assert ["no_egon_bus" in f for f in table["flags"]] == [False, True, False, False]
    assert not any("no_egon_bus" in f for f in sites_frame(sites)["flags"])           # no cross-check: no flag
    assert table["osm_ratings_mva"].tolist() == [None, None, None, [40.0, 40.0]]
    assert table["source"].unique().tolist() == ["osm"]
    assert table["footprint_wkb"].iloc[3] is not None and table["footprint_wkb"].iloc[0] is None
    assert sites_frame(sites)["in_region"].tolist() == [None] * 4
    assert list(sites_frame([]).columns) == list(SITE_COLUMNS)
    with pytest.raises(ValueError, match="matches"):
        sites_frame(sites, region, [(None, None)])


def test_ratings_of_hv_mv_transformers_only_count_as_known_units():
    rows = pd.DataFrame({
        "site_index": [0, 0, 0, 0, 1, 2],
        "voltage": ["110000;20000", "110000;20000", "220000;110000", None, "110000", "110000;20000"],
        "rating": ["40 MVA", "40MVA", "200 MVA", "63 MVA", "31,5 MVA", "n/a"],
    })
    ratings = site_ratings(rows, 3)
    assert ratings == [(40.0, 40.0), (31.5,), ()]
    assert known_units_mva(ratings[0]) == (40.0, 40.0)
    assert known_units_mva(ratings[1]) == ()
    assert site_ratings(rows.iloc[0:0], 2) == [(), ()]


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
    row = ("way/1", "110000;20000", None, "50", "Netz", "UW", None, 1.0, 2.0, 400.0, memoryview(footprint), True)
    client = _client(lambda text, params: [row])
    region = shapely.to_wkb(shapely.box(0, 0, 10, 10))
    result = client.osm_substation_candidates(memoryview(region), 5000)
    text, params = client.cur.calls[0]
    for needle in ('"osm_x".infrastructure_point', '"osm_x".infrastructure_polygon', '"osm_x".tags',
                   "t.geom_type = c.gt AND t.osm_id = c.tag_id", "osm_subtype IN ('plant', 'generator')",
                   "ST_DWithin(c.pt, ST_GeomFromWKB(%(region)s, 25832), %(buffer_m)s)", "'relation/' ||"):
        assert needle in text
    assert params == {"region": region, "buffer_m": 5000.0}
    assert isinstance(params["region"], bytes)
    assert tuple(result.columns) == CANDIDATE_COLUMNS == source_mixin.OSM_CANDIDATE_COLUMNS
    assert result["footprint_wkb"].iloc[0] == footprint and isinstance(result["footprint_wkb"].iloc[0], bytes)
    assert result["in_plant"].dtype == bool and result["x"].dtype == float
    assert classify_candidate(result.iloc[0]) == (False, "in_plant")

    empty = _client(lambda text, params: []).osm_substation_candidates(region, 5000)
    assert tuple(empty.columns) == CANDIDATE_COLUMNS and len(empty) == 0


def test_osm_queries_need_the_osm_schema(schemas, monkeypatch):
    monkeypatch.setattr(source_mixin, "INFDB_OSM_SCHEMA", None)
    client = _client(lambda text, params: [])
    with pytest.raises(MvStageError, match="INFDB_OSM_SCHEMA"):
        client.osm_substation_candidates(b"x", 5000)
    with pytest.raises(MvStageError, match="INFDB_OSM_SCHEMA"):
        client.osm_transformer_ratings([b"x"])
    assert client.cur.calls == []


def test_transformer_ratings_keep_the_site_positions(schemas):
    client = _client(lambda text, params: [(1, "node/9", "110000;20000", "40 MVA")])
    result = client.osm_transformer_ratings([None, memoryview(b"ab"), None])
    text, params = client.cur.calls[0]
    assert "unnest(%(footprints)s::bytea[]) WITH ORDINALITY" in text and "osm_subtype = 'transformer'" in text
    assert params == {"footprints": [None, b"ab", None], "margin_m": 10.0}
    assert tuple(result.columns) == source_mixin.OSM_RATING_COLUMNS
    assert site_ratings(result, 3) == [(), (40.0,), ()]
    nothing = _client(lambda text, params: [])
    assert len(nothing.osm_transformer_ratings([None, None])) == 0 and nothing.cur.calls == []


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


def test_egon_tables_prefer_filled_pylovo_tables_then_opendata(schemas):
    both = {"pylovo.egon_hvmv_substation": True, "pylovo.egon_mv_grid_district": True,
            "open_x.egon_hvmv_substation": True, "open_x.egon_mv_grid_district": True}
    assert _client(_egon_responder(both)).egon_available() == (True, "pylovo")
    empty_pylovo = {**both, "pylovo.egon_hvmv_substation": False, "pylovo.egon_mv_grid_district": False}
    assert _client(_egon_responder(empty_pylovo)).egon_available() == (True, "open_x")
    mixed = {**both, "pylovo.egon_mv_grid_district": False}
    assert _client(_egon_responder(mixed)).egon_available() == (True, "pylovo (substations), open_x (districts)")
    ok, reason = _client(_egon_responder({"open_x.egon_hvmv_substation": True})).egon_available()
    assert not ok and "egon_mv_grid_district" in reason and "populate the eGon-data tables" in reason


def test_egon_readers_map_na_to_none_and_fail_without_tables(schemas):
    present = {"open_x.egon_hvmv_substation": True, "open_x.egon_mv_grid_district": True}
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
    assert [s.max_v for s in egon_sites(result)] == [110000, 110000]

    districts = _client(_egon_responder(present, [(501, memoryview(b"wkb"))])).egon_districts(b"region")
    assert districts["geom_wkb"].tolist() == [b"wkb"] and tuple(districts.columns) == ("bus_id", "geom_wkb")

    missing = _client(_egon_responder({}))
    with pytest.raises(MvStageError, match="no eGon-data table egon_hvmv_substation"):
        missing.egon_substations(b"region", 5000)
    with pytest.raises(MvStageError, match="egon_mv_grid_district"):
        missing.egon_districts(b"region")


def test_mastr_readers(schemas):
    client = _client(lambda text, params: [("SEE1", "SEL1", "wind", None, 3000.0, "Mittelspannung", 1.0, 2.0)])
    units = client.mastr_units_in_region(memoryview(b"region"))
    text, params = client.cur.calls[0]
    assert "FROM pylovo.mastr_units u" in text and "technology IS DISTINCT FROM 'storage'" in text
    assert "ST_Within(u.geom, ST_GeomFromWKB(%(region)s, 25832))" in text and "net_power_kw > 0" in text
    assert params == {"region": b"region"}
    assert tuple(units.columns) == source_mixin.MASTR_UNIT_COLUMNS and units["net_power_kw"].dtype == float

    client = _client(lambda text, params: [("01234", "solar", 12, 345.6, 0.5), ("99998", "wind", 1, 50.0, 1.0)])
    aggregates = client.mastr_postcode_aggregates([99998, 1234, 99998])
    text, params = client.cur.calls[0]
    assert params == {"plz_text": ["01234", "99998"]}
    assert "lpad(p.plz::text, 5, '0') = a.postcode" in text
    assert tuple(aggregates.columns) == source_mixin.MASTR_AGGREGATE_COLUMNS
    assert aggregates["plz"].tolist() == [1234, 99998] and aggregates["area_ratio"].tolist() == [0.5, 1.0]
    assert len(_client(lambda text, params: []).mastr_postcode_aggregates([])) == 0


def test_mastr_available(schemas):
    def respond(count):
        return lambda text, params: [(True,)] if "to_regclass" in text else [(count,)]
    assert _client(respond(1234)).mastr_available() == (True, "1234 units")
    ok, reason = _client(respond(0)).mastr_available()
    assert not ok and "pylovo-import mastr" in reason
    ok, reason = _client(lambda text, params: [(False,)]).mastr_available()
    assert not ok and "pylovo-setup" in reason
