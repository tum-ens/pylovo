"""Municipal register: a municipality without a RegioStaR row keeps its (PLZ, AGS) pair with empty classes."""
import numpy as np
import pandas as pd
import pytest

import pylovo.classification.sampling.sample as sample
from classification_helpers import ddl_columns
from pylovo.data_import.municipal_register import join_regiostar_plz
from pylovo.data_import.region_resolver import resolve_regions
from pylovo.database.config_table_structure import CREATE_QUERIES

NEW_MUNICIPALITY = 16065089     # in the PLZ assignment, but not in the RegioStaR table of 2020


def sources():
    plz_pop = pd.DataFrame({"plz": [80331, 99718], "note": ["80331 Town A", "99718 Town B"],
                            "population": [20000, 5000], "qkm": [4.0, 50.0], "lat": [48.1, 51.3],
                            "lon": [11.6, 10.9]})
    plz_ags = pd.DataFrame({"ags": [9162000, NEW_MUNICIPALITY], "ort": ["Town A", "New municipality B"],
                            "plz": [80331, 99718], "landkreis": ["District A", "District B"],
                            "bundesland": ["Bayern", "Thüringen"]})
    regiostar = pd.DataFrame({"mun_code": [9162000, 9162001], "name_city": ["Town A (RegioStaR)", "Town C"],
                              "pop": [1500000, 1000], "area": [310.7, 5.0], "fed_state": [9, 9],
                              "regio7": [71, 75], "regio5": [51, 53], "pop_den": [4800.0, 200.0]})
    return plz_pop, plz_ags, regiostar


def test_the_pair_without_regiostar_row_stays_with_empty_classes_and_the_name_of_the_plz_file():
    register = join_regiostar_plz(*sources()).set_index("ags")
    assert len(register) == 2 and set(register.columns) | {"ags"} == set(ddl_columns(CREATE_QUERIES["municipal_register"]))
    new, known = register.loc[NEW_MUNICIPALITY], register.loc[9162000]
    assert new[["fed_state", "regio7", "regio5"]].isna().all() and new["name_city"] == "New municipality B"
    assert (known["fed_state"], known["regio7"], known["regio5"], known["name_city"]) == (9, 71, 51, "Town A (RegioStaR)")
    assert (new["pop"], new["area"], new["pop_den"]) == (5000, 50.0, 100.0)    # PLZ values, not municipal ones
    assert str(register["regio7"].dtype) == "Int64"                           # to_sql writes integers and NULL


class Client:
    def __init__(self, register):
        self.register = register

    def get_municipal_register(self):
        return self.register


def database_register() -> pd.DataFrame:
    """The register as the database hands it over: Python integers and None for the missing classes."""
    rng = np.random.default_rng(3)
    rows = [(81000 + n, int(rng.integers(500, 30000)), float(rng.uniform(2, 80)), 48.0, 11.0, 9000000 + n, f"c{n}", 9,
             71 if n % 2 else 75, 51, 0.0) for n in range(60)]
    rows += [(99700 + n, 3000, 40.0, 51.0, 10.0, 16000000 + n, f"new{n}", None, None, None, 75.0) for n in range(3)]
    register = pd.DataFrame(rows, columns=["plz", "pop", "area", "lat", "lon", "ags", "name_city", "fed_state",
                                           "regio7", "regio5", "pop_den"])
    register["pop_den"] = register["pop"] / register["area"]
    return register


def test_the_sampling_ignores_plz_without_a_class(monkeypatch):
    monkeypatch.setattr(sample, "N_SAMPLES", 10)
    register = database_register()
    assert register["regio7"].isna().sum() == 3
    shares = sample.perc_of_pop_per_class(register)
    assert set(shares) == {71, 75} and all(n >= 1 for n in shares.values())
    np.random.seed(1)
    drawn = sample.get_samples_with_regiostar({71: 4, 75: 4}, register)
    assert len(drawn) == 8 and not set(drawn["plz"]) & {99700, 99701, 99702}


@pytest.mark.parametrize("request_", [{"plz": 99701}, {"ags": 16000001}])
def test_a_plz_or_ags_without_a_class_resolves(request_):
    plz_list, rows = resolve_regions(Client(database_register()), **request_)
    assert plz_list == [99701] and rows["ags"].tolist() == [16000001] and rows["regio7"].isna().all()
