"""Report how many grids each stage of the classification kept and why the others dropped out."""
from collections.abc import Sequence

import pandas as pd

from pylovo.classification.database_communication.database_communication import DatabaseCommunication, filter_reasons
from pylovo.config_loader import CLASSIFICATION_VERSION, VERSION_ID


def summarize_grid_counts(
    grids: pd.DataFrame, filters: Sequence[str], plz_without_grids: Sequence[int] = ()
) -> tuple[dict, list[str]]:
    """Count the grids of the sample per stage and describe every loss that the filters do not explain.

    Args:
        grids: Status of the grids of the sampled PLZ, see
            :meth:`DatabaseCommunication.get_grid_status_of_sample_set`.
        filters: Labels of the filters that applied (boolean columns of ``grids``).
        plz_without_grids: PLZ that were drawn but removed from the sample set because no grids could be
            generated.

    Returns:
        The counts (``plz_drawn``, ``plz_in_sample``, ``grids``, ``analysed``, ``filtered``, ``filtered_by``
        per label and ``clustered``) and the warnings, one text per kind of loss.
    """
    has_grid = grids["grid_result_id"].notna()
    analysed = has_grid & grids["analysed"].astype(bool)
    matched = {label: analysed & grids[label].astype(bool) for label in filters}
    filtered = pd.DataFrame(matched, index=grids.index).any(axis=1)
    plz_in_sample = int(grids["plz"].nunique())
    counts = {
        "plz_drawn": plz_in_sample + len(plz_without_grids),
        "plz_in_sample": plz_in_sample,
        "grids": int(has_grid.sum()),
        "analysed": int(analysed.sum()),
        "filtered": int(filtered.sum()),
        "filtered_by": {label: int(mask.sum()) for label, mask in matched.items()},
        "clustered": int((has_grid & grids["clustered"].astype(bool)).sum()),
    }

    warnings = []
    if plz_without_grids:
        warnings.append(f"{len(plz_without_grids)} of {counts['plz_drawn']} sampled PLZ have no grids and were "
                        f"removed from the sample set: {sorted(int(plz) for plz in plz_without_grids)}")
    no_grid_plz = sorted(int(plz) for plz in set(grids["plz"]) - set(grids.loc[has_grid, "plz"]))
    if no_grid_plz:
        warnings.append(f"Sampled PLZ without any grid for VERSION_ID {VERSION_ID}: {no_grid_plz}")
    without_figures = has_grid & ~analysed
    if without_figures.any():
        no_plz_analysis = without_figures & ~grids["plz_analysed"].astype(bool)
        failed = without_figures & ~no_plz_analysis
        reasons = []
        if no_plz_analysis.any():
            plz = sorted(int(p) for p in grids.loc[no_plz_analysis, "plz"].unique())
            reasons.append(f"{int(no_plz_analysis.sum())} in {len(plz)} PLZ without PLZ analysis "
                           f"(run pylovo-analyze --plz {' '.join(str(p) for p in plz)})")
        if failed.any():
            reasons.append(f"{int(failed.sum())} whose calculation failed (see the log)")
        warnings.append(f"{int(without_figures.sum())} of {counts['grids']} grids have no key figures and are not "
                        f"clustered: {', '.join(reasons)}")
    kept = counts["analysed"] - counts["filtered"]
    if counts["clustered"] != kept:
        warnings.append(f"{kept} grids pass the filters, but {counts['clustered']} are in transformer_classified "
                        f"of classification {CLASSIFICATION_VERSION}")
    return counts, warnings


def filtered_by_class(grids: pd.DataFrame, filters: Sequence[str]) -> pd.DataFrame:
    """What the filters leave out per RegioStaR 7 class, and the small grids that stay in.

    The classes of the clustering must stand for all grids of the region, so the share that the filters remove is
    reported, in grids and in the buildings and households they supply. Grids with one building and grids without
    households are counted among the grids that pass the filters: they stay in the clustering.

    Args:
        grids: Status of the grids of the sampled PLZ (:meth:`DatabaseCommunication.get_grid_status_of_sample_set`).
        filters: Labels of the filters that applied (boolean columns of ``grids``).

    Returns:
        pd.DataFrame: One row per class and a row ``all``: ``grids`` (with key figures), ``filtered``,
        ``filtered_pct``, ``buildings_filtered``, ``households_filtered``, one column ``F1``, ``F2``, ... per filter
        (grids, in the order of ``filters``), ``one_building`` and ``no_households``.
    """
    analysed = grids[grids["grid_result_id"].notna() & grids["analysed"].astype(bool)].copy()
    matched = analysed[list(filters)].astype(bool) if filters else pd.DataFrame(index=analysed.index)
    filtered = matched.any(axis=1) if filters else pd.Series(False, index=analysed.index)
    kept = ~filtered
    table = pd.DataFrame({
        "regio7": analysed["regio7"].fillna(-1).astype(int),
        "grids": 1,
        "filtered": filtered.astype(int),
        "buildings_filtered": analysed["buildings"].where(filtered, 0).fillna(0),
        "households_filtered": analysed["households"].where(filtered, 0).fillna(0),
        **{f"F{i}": matched[label].astype(int) for i, label in enumerate(filters, start=1)},
        "one_building": (kept & (analysed["buildings"] == 1)).astype(int),
        "no_households": (kept & (analysed["households"] == 0)).astype(int),
    })
    by_class = table.groupby("regio7").sum()
    by_class.loc["all"] = by_class.sum()
    by_class = by_class.astype(int)
    by_class.insert(2, "filtered_pct", (100 * by_class["filtered"] / by_class["grids"].where(by_class["grids"] > 0)).round(1))
    return by_class.rename(index={-1: "?"})


def report_grid_counts(additional_filtering: bool | None = None, plz_without_grids: Sequence[int] = ()) -> dict:
    """Print how many grids each stage of the classification kept and log a warning for every loss.

    Run it after step 7 (``apply_clustering_for_visualisation``). The stages are the sampled PLZ, their
    grids, the grids with key figures, the grids the filters leave out and the grids in
    ``transformer_classified``. Grids without key figures (the PLZ analysis is missing or the calculation failed)
    and a mismatch with the result table are warnings, the filters are listed per rule and, per RegioStaR 7 class,
    with the buildings and households of the grids they leave out (:func:`filtered_by_class`). The power-flow
    filter follows the choice of the classification.

    Args:
        additional_filtering: Whether the lower thresholds apply. ``None`` uses the choice of the classification
            (``DatabaseCommunication.get_classification_choices``).
        plz_without_grids: The PLZ that ``prepare_data_for_clustering`` removed from the sample set.

    Returns:
        dict: The counts of :func:`summarize_grid_counts` and the table of :func:`filtered_by_class` (``by_class``).
    """
    dc = DatabaseCommunication()
    choices = dc.get_classification_choices()
    if additional_filtering is None:
        additional_filtering = choices["additional_filtering"]
    exclude_infeasible = choices["exclude_infeasible"]
    grids = dc.get_grid_status_of_sample_set(additional_filtering, exclude_infeasible)
    filters = list(filter_reasons(additional_filtering, exclude_infeasible))
    counts, warnings = summarize_grid_counts(grids, filters, plz_without_grids)
    counts["by_class"] = filtered_by_class(grids, filters)
    rows = [("PLZ drawn", counts["plz_drawn"]), ("PLZ in the sample set", counts["plz_in_sample"]),
            ("Grids of these PLZ", counts["grids"]), ("Grids with key figures", counts["analysed"]),
            ("Grids filtered", counts["filtered"])]
    rows += [(f"  by {label}", n) for label, n in counts["filtered_by"].items() if n]
    rows.append(("Grids clustered", counts["clustered"]))
    width = max(len(label) for label, _ in rows)
    print(f"\nGrids of classification {CLASSIFICATION_VERSION} (VERSION_ID {VERSION_ID})")
    print("\n".join(f"  {label:<{width}}  {value:>6}" for label, value in rows))
    print("\nGrids with key figures per RegioStaR 7 class: left out by the filters (grids, buildings, households, "
          "per filter F) and kept with one building or without households")
    print(counts["by_class"].to_string())
    print("\n".join(f"  F{i}: {label}" for i, label in enumerate(filters, start=1)))
    for text in warnings:
        dc.dbc.logger.warning(text)
    return counts
