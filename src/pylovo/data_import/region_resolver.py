"""Resolve PLZ or AGS inputs against the municipal register.

Used by ``pylovo-generate`` to turn AGS inputs into PLZ lists and by
:mod:`pylovo.data_import.import_buildings` to find the AGS whose building shapefiles are needed.
PLZ and AGS are integers (no leading zeros); the register is read with
``dbc_client.get_municipal_register()``.
"""

from __future__ import annotations

from typing import Any

import pandas as pd


def _as_list(value: int | list[int] | None) -> list[int] | None:
    """Wrap a single code in a list; keep lists and ``None`` unchanged."""
    if value is None or isinstance(value, list):
        return value
    return [value]


def resolve_regions(
    dbc_client: Any,
    *,
    plz: int | list[int] | None = None,
    ags: int | list[int] | None = None,
) -> tuple[list[int], pd.DataFrame]:
    """Resolve regional inputs (PLZ or AGS) against the municipal register.

    Args:
        dbc_client: Database client with ``get_municipal_register()``.
        plz: One PLZ or a list of PLZ. Mutually exclusive with ``ags``.
        ags: One AGS or a list of AGS. Mutually exclusive with ``plz``.

    Returns:
        Tuple ``(plz_list, df_plz_ags)``: the sorted unique PLZ to generate grids for, and the
        matching ``municipal_register`` rows (needed to pick building shapefiles by AGS).

    Raises:
        ValueError: If not exactly one of ``plz`` and ``ags`` is given, or a code is missing in
            the municipal register.
        TypeError: If the database layer does not return a pandas DataFrame.
    """
    plz_list_in = _as_list(plz)
    ags_list_in = _as_list(ags)
    if (plz_list_in is None) == (ags_list_in is None):
        raise ValueError("resolve_regions() needs exactly one of 'plz' or 'ags'.")

    mr = dbc_client.get_municipal_register()
    if not isinstance(mr, pd.DataFrame):
        raise TypeError("dbc_client.get_municipal_register() must return a pandas DataFrame")

    # Filter by the chosen regional input and verify that all codes exist.
    if plz_list_in is not None:
        df_plz_ags = mr[mr["plz"].isin(plz_list_in)].copy()

        present_plz = set(df_plz_ags["plz"].tolist())
        missing_plz = sorted(set(plz_list_in).difference(present_plz))
        if missing_plz:
            raise ValueError(f"PLZ not found in municipal_register: {missing_plz}")

    else:
        df_plz_ags = mr[mr["ags"].isin(ags_list_in)].copy()

        present_ags = set(df_plz_ags["ags"].tolist())
        missing_ags = sorted(set(ags_list_in).difference(present_ags))
        if missing_ags:
            raise ValueError(f"AGS not found in municipal_register: {missing_ags}")

    plz_list_out = sorted(set(df_plz_ags["plz"].tolist()))

    return plz_list_out, df_plz_ags
