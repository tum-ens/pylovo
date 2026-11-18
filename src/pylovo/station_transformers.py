"""The MV/LV transformers of pylovo's stations: the units of a station rating and the data of each unit.

One table for every part of pylovo that builds or evaluates a station transformer: LV generation
(:meth:`pylovo.cable_installer.CableInstaller.create_transformer`), both electrical backends, the voltage budget of
the MV stage (:func:`pylovo.mv_grid.electrical.station_transformer`) and the MV coupling
(:func:`pylovo.mv_grid.coupling.upsize_station_transformer`).

**Units of a station** (:func:`station_units`). A rating that has its own unit type (:data:`UNIT_RATINGS_KVA`,
the standard sizes of EN 50588-1 that pylovo uses) is one transformer, as in a compact station. Any other rating
is ``int(rating / 630)`` units (at least one) of ``rating / units`` each: stations above the largest allowed
rating are planned as ``n x 630 kVA`` (:data:`MULTI_UNIT_KVA`, ``ClusteringMixin.update_transformer_rated_power``).

**Unit types** (:func:`unit_type`, 20/0.4 kV). pylovo models the transformers that are installed today, most of
them older than the Ecodesign limits (EU) No 548/2014 (2015, tightened 2021). All types therefore come from one
family of older units, pandapower's standard types (Trafo Union data sheets):

* 250, 400 and 630 kVA: pandapower's standard types (630 kVA: 6 % short-circuit voltage, 7.6 kW load losses,
  1.65 kW no-load losses; Ecodesign allows 6.5 / 0.6 kW from 2015 and 4.6 / 0.54 kW from 2021).
* 100 and 160 kVA: pandapower has none; the 0.25 MVA type with the rating replaced and the no-load losses scaled
  with the rating.
* 800 kVA and any other unit rating: the next larger type, else the largest (0.63 MVA), with the rating replaced and
  the no-load losses scaled with the rating (800 kVA: 9.6 kW load and 2.1 kW no-load losses; Ecodesign allows
  8.4 / 0.65 kW from 2015).
"""
from __future__ import annotations

import functools
import math
from typing import Any

#: Unit ratings (kVA) with their own type.
UNIT_RATINGS_KVA: tuple[float, ...] = (100.0, 160.0, 250.0, 400.0, 630.0, 800.0)
#: Unit rating (kVA) of the stations above the largest allowed rating (``n x 630 kVA``).
MULTI_UNIT_KVA: float = 630.0
#: The type that the units below 250 kVA are derived from.
_SMALL_UNIT_BASE_KVA: float = 250.0


def station_units(rating_kva: float) -> tuple[float, int]:
    """``(unit kVA, units)`` of a station rating; the units always total the rating."""
    rating = float(rating_kva)
    if any(math.isclose(rating, r) for r in UNIT_RATINGS_KVA):
        return rating, 1
    units = max(1, int(rating / MULTI_UNIT_KVA + 1e-9))
    return rating / units, units


def type_name(unit_kva: float) -> str:
    """The pandapower ``std_type`` name of a unit (``0.63 MVA 20/0.4 kV``)."""
    return f"{float(unit_kva) / 1000.0} MVA 20/0.4 kV"


def _scaled(base: dict[str, Any], sn_mva: float) -> dict[str, Any]:
    """``base`` with the rating ``sn_mva`` and its no-load losses scaled with the rating."""
    return dict(base, sn_mva=sn_mva, pfe_kw=base["pfe_kw"] * sn_mva / base["sn_mva"])


@functools.cache
def _types() -> dict[float, dict[str, Any]]:
    from pandapower.std_types import basic_trafo_std_types   # imported on first use: LV planning needs no pandapower

    pandapower = basic_trafo_std_types()
    types = {kva: dict(pandapower[type_name(kva)]) for kva in (250.0, 400.0, 630.0)}
    for kva in (100.0, 160.0):
        types[kva] = _scaled(types[_SMALL_UNIT_BASE_KVA], kva / 1000.0)
    return dict(sorted(types.items()))


def unit_type(unit_kva: float) -> tuple[str, dict[str, Any]]:
    """``(std_type name, pandapower type data)`` of a station unit (a copy: change it freely)."""
    unit = float(unit_kva)
    types = _types()
    for kva, data in types.items():
        if math.isclose(unit, kva):
            return type_name(kva), dict(data)
    larger = [kva for kva in types if kva >= unit]
    base = types[larger[0] if larger else max(types)]
    return type_name(unit), _scaled(base, unit / 1000.0)
