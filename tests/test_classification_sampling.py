"""Tests of ``classification/sampling/sample.py`` with a synthetic municipal register and a fake database client."""
import numpy as np
import pandas as pd
import pytest

import pylovo.classification.sampling.sample as sample
from classification_helpers import FakeCursor, FakeDbc, ddl_columns
from pylovo.database.config_table_structure import CREATE_QUERIES


def register(n=400, seed=0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({
        "plz": 80000 + np.arange(n), "pop": rng.integers(500, 30000, n), "area": rng.uniform(2, 80, n),
        "lat": rng.uniform(47, 50, n), "lon": rng.uniform(9, 13, n), "ags": 9000000 + np.arange(n),
        "name_city": [f"c{i}" for i in range(n)], "fed_state": 9,
        "regio7": rng.choice([71, 72, 73, 74, 75, 76, 77], n, p=[.05, .1, .3, .1, .05, .2, .2]), "regio5": 51,
    })
    df["pop_den"] = df["pop"] / df["area"]
    return df


class FakeClient(FakeDbc):
    def __init__(self, reg, version_exists=0, available_plz=None):
        fetchall = [[(int(p),) for p in available_plz]] if available_plz is not None else []
        super().__init__(FakeCursor(fetchone_values=[(version_exists,)], fetchall_values=fetchall))
        self.reg = reg

    def get_municipal_register(self):
        return self.reg.copy()


@pytest.fixture()
def written(monkeypatch):
    """Capture the frame that ``sample_set_to_db`` hands to ``to_sql``."""
    captured = {}

    def fake_to_sql(self, name, con=None, **kw):
        captured.update(name=name, frame=self.copy(), kw=kw)

    monkeypatch.setattr(pd.DataFrame, "to_sql", fake_to_sql)
    monkeypatch.setattr(sample, "CLASSIFICATION_VERSION", 7)
    monkeypatch.setattr(sample, "CLASSIFICATION_REGION", "Bayern")
    monkeypatch.setattr(sample, "N_SAMPLES", 40)
    return captured


def test_samples_per_class_follow_population_shares(monkeypatch):
    monkeypatch.setattr(sample, "N_SAMPLES", 100)
    reg = register()
    counts = sample.perc_of_pop_per_class(reg)
    share = reg.groupby("regio7")["pop"].sum() / reg["pop"].sum()
    assert set(counts) == set(share.index)
    for cls, n in counts.items():
        assert abs(n - share[cls] * 100) <= 0.5 + 1e-9
    assert abs(sum(counts.values()) - 100) <= len(counts) / 2  # rounding per class


def test_class_sample_is_unique_sized_and_uses_valid_probabilities(monkeypatch):
    reg = register()
    seen = {}
    original = np.random.choice

    def spy(a, size, p=None, replace=True):
        seen["p"] = np.asarray(p)
        return original(a, size, p=p, replace=replace)

    monkeypatch.setattr(np.random, "choice", spy)
    np.random.seed(5)
    out = sample.get_samples_within_regiostar_class(73, 12, reg)
    assert len(out) == 12 and out["plz"].is_unique
    assert set(out["plz"]) <= set(reg.loc[reg["regio7"] == 73, "plz"])
    assert seen["p"].sum() == pytest.approx(1.0) and (seen["p"] > 0).all()
    assert len(seen["p"]) == (reg["regio7"] == 73).sum()


def test_class_with_fewer_grids_than_requested_raises():
    reg = register()
    n_class = int((reg["regio7"] == 71).sum())
    with pytest.raises(ValueError, match="larger sample than population"):
        sample.get_samples_within_regiostar_class(71, n_class + 1, reg)


@pytest.mark.parametrize("bad_value", [np.nan, np.inf])
def test_non_finite_population_density_is_rejected(bad_value):
    reg = register()
    reg.loc[reg.index[0], "pop_den"] = bad_value
    with pytest.raises(ValueError, match="not finite"):
        sample.get_samples_with_regiostar({int(reg.loc[reg.index[0], "regio7"]): 2}, reg)


def test_create_sample_set_writes_the_sample_set_columns(monkeypatch, written):
    reg = register()
    client = FakeClient(reg)
    monkeypatch.setattr(sample, "_db_client", client)
    np.random.seed(11)
    sample.create_sample_set()
    frame = written["frame"]
    assert written["name"] == "sample_set"
    assert list(frame.columns) == ddl_columns(CREATE_QUERIES["sample_set"])
    assert (frame["classification_id"] == 7).all()
    assert frame["plz"].is_unique and set(frame["plz"]) <= set(reg["plz"])
    assert frame.merge(reg, on=["plz", "ags"]).shape[0] == len(frame), "every (plz, ags) exists in the register"
    insert = [s for s in client.cur.executed if s[0].startswith("INSERT INTO pylovo.classification_version")]
    assert len(insert) == 1 and insert[0][1]["region"] == "Bayern" and insert[0][1]["c"] == 7


def test_existing_version_is_rejected_without_any_write(monkeypatch, written):
    client = FakeClient(register(), version_exists=1)
    monkeypatch.setattr(sample, "_db_client", client)
    with pytest.raises(Exception, match="already exists"):
        sample.create_sample_set()
    assert not any(s[0].startswith("INSERT") for s in client.cur.executed) and not written


def test_restriction_to_postcode_result_limits_the_pool(monkeypatch, written):
    reg = register()
    pool = reg["plz"].iloc[:120]
    client = FakeClient(reg, available_plz=pool)
    monkeypatch.setattr(sample, "_db_client", client)
    np.random.seed(2)
    sample.create_sample_set(restrict_to_postcode_result=True)
    assert set(written["frame"]["plz"]) <= set(pool)


def test_federal_state_id():
    assert sample.get_federal_state_id() == 9
    sample.CLASSIFICATION_REGION, old = "Nowhere", sample.CLASSIFICATION_REGION
    try:
        with pytest.raises(IndexError):
            sample.get_federal_state_id()
    finally:
        sample.CLASSIFICATION_REGION = old


def test_get_sample_set_columns_match_the_municipal_register_order():
    """get_sample_set builds the frame positionally with MUNICIPAL_REGISTER as column names."""
    import inspect
    import re

    from pylovo.config_loader import MUNICIPAL_REGISTER

    source = inspect.getsource(sample.get_sample_set)
    select = re.search(r"SELECT (.*?)\s+FROM", source, re.S).group(1)
    assert [c.strip().split(".")[-1] for c in select.split(",")] == MUNICIPAL_REGISTER


@pytest.mark.xfail(strict=True, reason="CLASSIF-05: the classification version is committed before sampling can fail")
def test_failed_sampling_leaves_no_registered_version(monkeypatch, written):
    client = FakeClient(register())
    monkeypatch.setattr(sample, "_db_client", client)
    monkeypatch.setattr(sample, "CLASSIFICATION_REGION", "Bayern ")  # typo: not a key of REGION_DICT
    with pytest.raises(IndexError):
        sample.create_sample_set()
    inserted = any(s[0].startswith("INSERT INTO pylovo.classification_version") for s in client.cur.executed)
    deleted = any(s[0].startswith("DELETE FROM pylovo.classification_version") for s in client.cur.executed)
    assert not inserted or deleted or client.conn.commits == 0


@pytest.mark.xfail(strict=True, reason="CLASSIF-08: np.random.choice uses the global, unseeded generator")
def test_sample_is_reproducible_for_the_same_version(monkeypatch, written):
    draws = []
    for _ in range(2):
        monkeypatch.setattr(sample, "_db_client", FakeClient(register()))
        sample.create_sample_set()
        draws.append(sorted(written["frame"]["plz"]))
    assert draws[0] == draws[1]
