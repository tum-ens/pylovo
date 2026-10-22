"""One ``equipment_data`` row per cable name, with every role of the cable (``insert_equipment_data_from_config``)."""

import logging

import pandas as pd
import pytest

from pylovo.database.preprocessing_mixin import PreprocessingMixin

CABLE = {"name": "NAYY_4_150", "max_i_a": 270, "r_mohm_per_km": 208, "x_mohm_per_km": 80, "z_mohm_per_km": 223,
         "cost_eur": 25, "typ": "Cable"}


class Recorder:
    """Stands in for the database client: records the upserted rows."""

    def __init__(self):
        self.rows = None
        self.cur = self
        self.conn = self
        self.logger = logging.getLogger("equipment-roles-test")

    def executemany(self, _sql, rows):
        self.rows = rows

    def commit(self):
        pass

    def rollback(self):
        pass


def equipment(*rows):
    return pd.DataFrame([{**row, "version_id": "t1"} for row in rows])


def test_a_cable_in_both_pools_is_one_row_with_both_roles():
    db = Recorder()
    PreprocessingMixin.insert_equipment_data_from_config(db, equipment(
        {"name": "Tr_400", "s_max_kva": 400, "cost_eur": 1, "typ": "Transformer", "grid_role": "transformer"},
        {**CABLE, "grid_role": "feeder"},
        {**CABLE, "name": "NAYY_4_50", "max_i_a": 142, "grid_role": "consumer_connection"},
        {**CABLE, "grid_role": "consumer_connection"},
    ))

    assert [(row["name"], row["grid_role"], row["max_i_a"]) for row in db.rows] == [
        ("Tr_400", "transformer", None),
        ("NAYY_4_150", "feeder,consumer_connection", 270),
        ("NAYY_4_50", "consumer_connection", 142),
    ]


def test_a_cable_listed_twice_with_different_values_is_refused():
    db = Recorder()
    with pytest.raises(ValueError, match="NAYY_4_150"):
        PreprocessingMixin.insert_equipment_data_from_config(db, equipment(
            {**CABLE, "grid_role": "feeder"}, {**CABLE, "cost_eur": 30, "grid_role": "consumer_connection"},
        ))
    assert db.rows is None
