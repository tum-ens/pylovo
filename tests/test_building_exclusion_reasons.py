"""The removal steps record the right exclusion reason (SQL text, no database; tests/test_postgis_fixtures.py runs them)."""

from pylovo.database.utils_mixin import UtilsMixin


class Cursor:
    rowcount = 0

    def __init__(self):
        self.statements = []

    def execute(self, query, params=None):
        self.statements.append(" ".join(query.split()))


def client_of(mixin):
    client = mixin.__new__(mixin)
    client.cur = Cursor()
    return client


def test_an_mv_direct_building_of_a_dropped_street_component_is_recorded_as_mv_direct():
    client = client_of(UtilsMixin)
    client.delete_transformers_from_buildings_tem([7])
    assert "CASE WHEN peak_load_in_kw = 0 THEN 'mv_direct' ELSE 'isolated_component' END" in client.cur.statements[0]


def test_a_building_with_a_transformer_inside_is_recorded_as_mv_direct_when_it_is_an_mv_customer():
    from pylovo.database.preprocessing_mixin import PreprocessingMixin

    client = client_of(PreprocessingMixin)
    client.remove_non_residential_buildings_overlapping_transformers()
    statement = client.cur.statements[0]
    assert "RETURNING b.objectid, b.nonresidential_mv_direct" in statement
    assert "CASE WHEN nonresidential_mv_direct THEN 'mv_direct' ELSE 'overlaps_transformer' END" in statement
