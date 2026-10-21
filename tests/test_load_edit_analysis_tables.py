"""The load editor removes and restores only analysis tables that the schema has and something writes."""
from pylovo.database.config_table_structure import CREATE_QUERIES
from pylovo.database.load_edit_mixin import _ANALYSIS_TABLES


def test_the_analysis_rows_of_an_edit_are_those_of_plz_parameters_and_clustering_parameters():
    tables = [table for table, _ in _ANALYSIS_TABLES]
    assert tables == ["plz_parameters", "clustering_parameters"]
    assert all(table in CREATE_QUERIES for table in tables)
