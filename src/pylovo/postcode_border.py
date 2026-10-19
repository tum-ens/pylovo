"""Postcode borders: modes, halo width and the shared rules of ``POSTCODE_BORDER_MODE``.

Grids are generated per PLZ. With ``halo`` a PLZ also routes over the InfDB streets within
:func:`halo_width_m` outside its polygon, so routes may pass through the neighbouring PLZ;
existing stations and buildings stay per PLZ. ``transfer`` (generating a border building in the
PLZ of its station) is not implemented yet.
"""
from __future__ import annotations

from pylovo.config_loader import MAX_BROWNFIELD_TRAFO_DISTANCE, POSTCODE_BORDER_MODE

MODES = ("off", "halo", "transfer")
#: Added to half the brownfield distance; covers geometric noise and short connection lines.
HALO_MARGIN_M = 100.0
#: Version of the halo rule, recorded in the version snapshot.
HALO_RULE_REVISION = 1


def halo_width_m() -> float:
    """Width of the street halo outside a PLZ.

    Any route of length L lies within L/2 of one of its two ends. With both ends inside the PLZ,
    half the brownfield distance plus a margin therefore holds every brownfield route of at most
    ``MAX_BROWNFIELD_TRAFO_DISTANCE``. Longer greenfield routes are not guaranteed to fit.
    """
    return float(MAX_BROWNFIELD_TRAFO_DISTANCE) / 2.0 + HALO_MARGIN_M


def halo_enabled(mode: str = POSTCODE_BORDER_MODE) -> bool:
    """Whether the PLZ loads the halo streets."""
    return mode in ("halo", "transfer")


def snapshot(mode: str = POSTCODE_BORDER_MODE) -> dict:
    """The effective border settings for the version snapshot (``None`` where the mode does not use them)."""
    on = halo_enabled(mode)
    return {
        "mode": mode,
        "halo_m": halo_width_m() if on else None,
        "halo_margin_m": HALO_MARGIN_M if on else None,
        "rule_revision": HALO_RULE_REVISION if on else None,
    }
