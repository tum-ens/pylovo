"""The helper that re-derives the lower filter thresholds follows the method behind the shipped values."""
import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml

import pylovo.classification.database_communication.database_communication as dcm
import pylovo.classification.utils.get_average_values_clustering_parameters as avg

SHIPPED = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "config_clustering.yaml").read_text())
FOUR = ["avg_trafo_dis", "no_house_connections", "vsw_per_branch", "no_households"]


def cluster_0_frame(values: dict) -> pd.DataFrame:
    """Two grids per parameter that are symmetric around ``values``, plus a parameter that has no threshold."""
    frame = pd.DataFrame({name: [value - 0.5, value + 0.5] for name, value in values.items()})
    frame["cable_length_km"] = [9.0, 11.0]
    return frame


def run_main(monkeypatch, capsys, frame, argv=(), clusters=None):
    seen = {}

    def fake_read(classification_id=None):
        seen["classification_id"] = classification_id
        return frame

    monkeypatch.setattr(avg, "get_clustering_parameters_for_kmeans_cluster_0", fake_read)
    monkeypatch.setattr(avg, "get_clustering_parameters_by_kmeans_cluster",
                        lambda classification_id=None: frame.assign(kmeans_clusters=0) if clusters is None else clusters)
    monkeypatch.setattr(sys, "argv", ["prog", *argv])
    avg.main()
    return seen, capsys.readouterr().out


def test_the_helper_derives_exactly_the_four_thresholds_of_the_filter():
    assert list(dcm.LOWER_FILTER_THRESHOLDS) == FOUR
    for parameter, threshold in dcm.LOWER_FILTER_THRESHOLDS.items():
        assert SHIPPED[f"THRESHOLD_{parameter.upper()}"] == threshold, "the printed key is the config key"


def test_the_means_of_cluster_0_reproduce_the_shipped_values():
    frame = cluster_0_frame({parameter: SHIPPED[f"THRESHOLD_{parameter.upper()}"] for parameter in FOUR})
    assert avg.calculate_average_clustering_parameters(frame, FOUR) == dcm.LOWER_FILTER_THRESHOLDS


def test_all_four_parameters_are_derived_although_the_clustering_uses_three(monkeypatch, capsys):
    """The old helper averaged LIST_OF_CLUSTERING_PARAMETERS, which has no no_households."""
    monkeypatch.setattr(avg, "LIST_OF_CLUSTERING_PARAMETERS", ["no_house_connections", "vsw_per_branch", "avg_trafo_dis"])
    frame = cluster_0_frame({"avg_trafo_dis": 0.1, "no_house_connections": 20.0, "vsw_per_branch": 0.3, "no_households": 50.0})
    _, out = run_main(monkeypatch, capsys, frame)
    assert out.splitlines()[1:5] == ["THRESHOLD_AVG_TRAFO_DIS: 0.1", "THRESHOLD_NO_HOUSE_CONNECTIONS: 20.0",
                                      "THRESHOLD_VSW_PER_BRANCH: 0.3", "THRESHOLD_NO_HOUSEHOLDS: 50.0"]
    assert "cable_length_km" not in out
    assert "Note: cluster 0 depends on the parameters of the clustering" in out


def test_the_means_are_rounded_to_three_decimals(monkeypatch, capsys):
    frame = pd.DataFrame({"avg_trafo_dis": [0.1, 0.12, 0.1334], "no_house_connections": [14, 15, 14],
                          "vsw_per_branch": [0.2, 0.3, 0.25], "no_households": [30, 40, 35]})
    _, out = run_main(monkeypatch, capsys, frame)
    assert "THRESHOLD_AVG_TRAFO_DIS: 0.118" in out and "THRESHOLD_NO_HOUSE_CONNECTIONS: 14.333" in out


def test_the_classification_is_an_option_and_defaults_to_the_configured_one(monkeypatch, capsys):
    frame = cluster_0_frame(dict.fromkeys(FOUR, 1.0))
    assert run_main(monkeypatch, capsys, frame, ["--classification-id", "42"])[0] == {"classification_id": 42}
    assert run_main(monkeypatch, capsys, frame)[0] == {"classification_id": avg.CLASSIFICATION_VERSION}


def test_only_cluster_0_of_one_classification_is_read(monkeypatch):
    seen = {}

    class FakeClient:
        sqla_engine = object()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_read(query, con=None, params=None):
        seen.update(query=" ".join(query.split()), params=params)
        return pd.DataFrame()

    monkeypatch.setattr(avg, "DatabaseClient", FakeClient)
    monkeypatch.setattr(avg.pd, "read_sql_query", fake_read)
    avg.get_clustering_parameters_for_kmeans_cluster_0(7)
    assert "WHERE tc.classification_id = %(c)s AND tc.kmeans_clusters = 0" in seen["query"] and seen["params"] == {"c": 7}
    avg.get_clustering_parameters_for_kmeans_cluster_0()
    assert seen["params"] == {"c": avg.CLASSIFICATION_VERSION}


def test_an_unknown_classification_stops_with_a_message(monkeypatch, capsys):
    with pytest.raises(SystemExit, match="no grids in k-means cluster 0"):
        run_main(monkeypatch, capsys, pd.DataFrame(columns=FOUR), ["--classification-id", "99"])


def test_the_means_of_every_cluster_show_whether_cluster_0_holds_the_small_grids(monkeypatch, capsys):
    """On 1.0.0 grids cluster 0 (fewest households of the representative) can be a cluster of long rural grids."""
    long_rural = {"avg_trafo_dis": 0.45, "no_house_connections": 22.0, "vsw_per_branch": 0.41, "no_households": 28.0}
    small = {"avg_trafo_dis": 0.115, "no_house_connections": 18.0, "vsw_per_branch": 0.2, "no_households": 39.0}
    clusters = pd.concat([cluster_0_frame(long_rural).assign(kmeans_clusters=0),
                          pd.concat([cluster_0_frame(small)] * 2).assign(kmeans_clusters=1)])
    _, out = run_main(monkeypatch, capsys, cluster_0_frame(long_rural), clusters=clusters)
    table = avg.cluster_means(clusters, FOUR)
    assert table.loc[0, "grids"] == 2 and table.loc[1, "grids"] == 4
    assert table.loc[1, FOUR].to_dict() == small and table.loc[0, "avg_trafo_dis"] == 0.45
    assert "Means of every k-means cluster" in out and table.to_string() in out
