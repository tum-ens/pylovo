"""Postcode borders: the neighboring streets of ``ROUTE_ACROSS_POSTCODE_BORDERS``.

Grids are generated per PLZ. With ``ROUTE_ACROSS_POSTCODE_BORDERS`` a PLZ also routes over the InfDB
streets of the neighboring PLZ within :func:`neighboring_street_width_m` outside its polygon, so routes
may pass through the neighboring PLZ; existing stations and buildings stay per PLZ.
"""
from __future__ import annotations

from pylovo.config_loader import MAX_BROWNFIELD_TRAFO_DISTANCE, ROUTE_ACROSS_POSTCODE_BORDERS

#: Added to half the brownfield distance; covers geometric noise and short connection lines.
NEIGHBORING_STREET_MARGIN_M = 100.0
#: Version of the rule, recorded in the version snapshot, so a version is not extended with grids of another rule.
#: 1: routing over the streets of the neighboring PLZ. 2: a PLZ also loads the connection lines of its buildings that
#: the InfDB files under a neighboring postcode.
RULE_REVISION = 2


def neighboring_street_width_m() -> float:
    """Width of the strip outside a PLZ whose streets the PLZ routes over.

    Any route of length L lies within L/2 of one of its two ends. With both ends inside the PLZ,
    half the brownfield distance plus a margin therefore holds every brownfield route of at most
    ``MAX_BROWNFIELD_TRAFO_DISTANCE``. Longer greenfield routes are not guaranteed to fit.
    """
    return float(MAX_BROWNFIELD_TRAFO_DISTANCE) / 2.0 + NEIGHBORING_STREET_MARGIN_M


def enabled() -> bool:
    """Whether a PLZ routes over the streets of its neighboring PLZ."""
    return ROUTE_ACROSS_POSTCODE_BORDERS


def snapshot(route_across: bool = ROUTE_ACROSS_POSTCODE_BORDERS) -> dict:
    """The effective border settings for the version snapshot (``None`` where routing stays inside the PLZ)."""
    return {
        "route_across_postcode_borders": bool(route_across),
        "neighboring_street_width_m": neighboring_street_width_m() if route_across else None,
        "neighboring_street_margin_m": NEIGHBORING_STREET_MARGIN_M if route_across else None,
        "rule_revision": RULE_REVISION if route_across else None,
    }
