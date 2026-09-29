"""Compare a version's stored generation-parameter snapshot with the one of the current config.

``pylovo-generate`` stores the generation parameters of a ``VERSION_ID`` once
(``PreprocessingMixin._generation_parameters_snapshot``, table ``pylovo.version``) and refuses to
add grids to that version with a different configuration. Parameters that were added to the
snapshot later are missing from the snapshots of older versions; :data:`ADDED_LATER` lists them
with the value that reproduces the behaviour from before they were recorded, or :data:`UNKNOWN`
when that behaviour depended on a setting that was not stored. A missing parameter then does not
block the version: with a known legacy value the current value must equal it, otherwise it is
only reported as *not recorded*.

Pure functions without configuration or database access (also used by ``pylovo-api``).
"""
from __future__ import annotations

from typing import Any

UNKNOWN = object()

#: Snapshot paths added after the first snapshots were stored, with their legacy value.
ADDED_LATER: dict[tuple[str, ...], Any] = {
    # Before this switch existed, manual positions were only used as part of the open positions.
    ("transformer_placement", "use_manual_transformer_positions"): False,
    # These config keys existed before they were recorded: the value used is not known.
    ("transformer_placement", "merge_greenfield_clusters"): UNKNOWN,
    ("transformer_placement", "greenfield_cluster_merge_transformer_kva"): UNKNOWN,
    # Before the station voltage existed the MV side was at 1.0 p.u.
    ("power_flow_assessment", "lv_reference_voltage_pu"): None,
}


def _flatten(value: Any, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    if isinstance(value, dict) and value:
        out: dict[tuple[str, ...], Any] = {}
        for key, item in value.items():
            out.update(_flatten(item, prefix + (str(key),)))
        return out
    return {prefix: value}


def _missing(stored: dict, path: tuple[str, ...]) -> bool:
    node: Any = stored
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return True
        node = node[key]
    return False


def compare_snapshots(stored: dict, expected: dict) -> tuple[list[str], list[str]]:
    """Differences between a stored and an expected snapshot (both JSON round-tripped).

    Returns:
        ``(differences, not_recorded)``: dotted paths whose values differ (these block the version),
        and paths of :data:`ADDED_LATER` that the stored snapshot does not contain and whose legacy
        value is unknown (these are only reported).
    """
    skipped: set[tuple[str, ...]] = set()
    not_recorded: list[str] = []
    differences: list[str] = []
    for path, legacy in ADDED_LATER.items():
        if not _missing(stored, path) or _missing(expected, path):
            continue
        skipped.add(path)
        current = expected
        for key in path:
            current = current[key]
        if legacy is UNKNOWN:
            not_recorded.append(".".join(path))
        elif current != legacy:
            differences.append(".".join(path))
    flat_stored, flat_expected = _flatten(stored), _flatten(expected)
    for path in sorted(set(flat_stored) | set(flat_expected)):
        if any(path[:len(s)] == s for s in skipped):
            continue
        if flat_stored.get(path, UNKNOWN) != flat_expected.get(path, UNKNOWN):
            differences.append(".".join(path))
    return sorted(set(differences)), not_recorded
