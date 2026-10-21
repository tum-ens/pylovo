"""Account for every source building of a set of PLZ: in a grid, left out with a reason, or missing.

The eligible buildings are the InfDB rows that generation loads (``InfdbClient.fetch_buildings_from_infdb``,
same filters). A building is

* ``generated``: in ``buildings_result`` of the version (in any PLZ's grid),
* ``excluded``: in ``building_exclusion`` with the step that left it out,
* ``postcode_not_generated``: its PLZ has no results in the version (failed, empty or not run),
* ``missing``: none of these, which a complete run must not have.

Rows of ``buildings_result``/``building_exclusion`` without an eligible source building are reported as
``not_in_source`` (the source changed or the filters differ).
"""
from __future__ import annotations

import pandas as pd

from pylovo.config_loader import VERSION_ID

#: Column positions in the rows of ``InfdbClient.fetch_buildings_from_infdb``.
_OBJECTID, _HOUSEHOLDS = 2, 14


def eligible_buildings(inf_dbc, plz_list: list[int]) -> pd.DataFrame:
    """The source buildings that generation loads for each PLZ (``objectid``, ``home_plz``, ``households``)."""
    rows = [(row[_OBJECTID], int(plz), row[_HOUSEHOLDS])
            for plz in plz_list for row in inf_dbc.fetch_buildings_from_infdb(int(plz))]
    frame = pd.DataFrame(rows, columns=["objectid", "home_plz", "households"])
    return frame.drop_duplicates("objectid")


def classify(eligible: pd.DataFrame, generated: pd.DataFrame, excluded: pd.DataFrame,
             postcodes_with_results: set[int]) -> pd.DataFrame:
    """One row per eligible or stored building with its ``status`` (and ``reason`` for exclusions).

    A building in ``generated`` or ``excluded`` that is not in ``eligible`` has the status ``not_in_source``; an
    exclusion keeps its ``reason`` and its PLZ in ``excluded_plz``.

    Args:
        eligible: ``objectid``, ``home_plz``, ``households`` of the source buildings.
        generated: ``objectid``, ``grid_plz``, ``grid_result_id`` from ``buildings_result``.
        excluded: ``objectid``, ``plz``, ``reason`` from ``building_exclusion``.
        postcodes_with_results: PLZ with a ``postcode_result`` row in the version.
    """
    frame = eligible.merge(generated, on="objectid", how="outer", indicator="_generated")
    frame = frame.merge(excluded.rename(columns={"plz": "excluded_plz"}), on="objectid", how="outer")
    in_source = frame["home_plz"].notna()
    is_generated = frame["_generated"].isin(["both", "right_only"])
    is_excluded = frame["reason"].notna()
    status = pd.Series("missing", index=frame.index, dtype=object)
    status[in_source & ~frame["home_plz"].isin(list(postcodes_with_results))] = "postcode_not_generated"
    status[is_excluded] = "excluded"
    status[is_generated] = "generated"
    status[is_generated & is_excluded] = "generated_and_excluded"
    status[~in_source] = "not_in_source"
    frame["status"] = status
    return frame.drop(columns="_generated")


def reconcile_coverage(dbc, inf_dbc, plz_list: list[int], version_id: str = VERSION_ID
                       ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Classify every source building of ``plz_list`` for ``version_id``.

    Returns:
        ``(buildings, summary)``: one row per building (see :func:`classify`), and the counts of
        buildings and households per home PLZ, status and reason.
    """
    plz_list = [int(plz) for plz in plz_list]
    eligible = eligible_buildings(inf_dbc, plz_list)
    generated = pd.read_sql_query(
        """SELECT br.objectid, gr.plz AS grid_plz, br.grid_result_id, br.households AS generated_households
           FROM pylovo.buildings_result br
           JOIN pylovo.grid_result gr ON gr.version_id = br.version_id AND gr.grid_result_id = br.grid_result_id
           WHERE br.version_id = %(v)s
             AND (gr.plz = ANY(%(p)s) OR br.objectid = ANY(%(o)s))""",
        con=dbc.conn, params={"v": version_id, "p": plz_list, "o": eligible["objectid"].tolist()})
    excluded = pd.read_sql_query(
        """SELECT objectid, plz, reason FROM pylovo.building_exclusion
           WHERE version_id = %(v)s AND (plz = ANY(%(p)s) OR objectid = ANY(%(o)s))""",
        con=dbc.conn, params={"v": version_id, "p": plz_list, "o": eligible["objectid"].tolist()})
    dbc.cur.execute("""SELECT postcode_result_plz FROM pylovo.postcode_result
                       WHERE version_id = %(v)s AND postcode_result_plz = ANY(%(p)s)""",
                    {"v": version_id, "p": plz_list})
    with_results = {int(row[0]) for row in dbc.cur.fetchall()}
    buildings = classify(eligible, generated, excluded, with_results)
    buildings["plz"] = buildings["home_plz"].fillna(buildings["grid_plz"]).fillna(buildings["excluded_plz"])
    summary = (buildings.assign(reason=buildings["reason"].fillna(""))
               .groupby(["plz", "status", "reason"], dropna=False)
               .agg(buildings=("objectid", "size"), households=("households", "sum"))
               .reset_index())
    return buildings, summary


def coverage_totals(buildings: pd.DataFrame) -> dict[str, int]:
    """Counts per status (and per exclusion reason) over all PLZ."""
    totals = buildings["status"].value_counts().to_dict()
    reasons = buildings.loc[buildings["status"] == "excluded", "reason"].value_counts().to_dict()
    return {**{status: int(count) for status, count in totals.items()},
            **{f"excluded:{reason}": int(count) for reason, count in reasons.items()}}
