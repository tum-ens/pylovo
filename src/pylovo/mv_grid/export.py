"""The export of a stored MV net (``pylovo-mv export``)."""
from __future__ import annotations

import logging
from pathlib import Path

from pylovo.mv_grid import runtime
from pylovo.mv_grid.model import CASES
from pylovo.mv_grid.parameters import MvParameters


def export_net(version_id: str, variant_id: str, mv_grid_id: int, output: Path, case: str = "load") -> Path:
    """Write the stored pandapower net of one MV grid to a JSON file (``pylovo-mv export``).

    Args:
        version_id: The LV version.
        variant_id: The MV variant.
        mv_grid_id: The district.
        output: The JSON file to write (its folder is created).
        case: ``load`` (the stored net with its load-case results), or another case of
            :data:`~pylovo.mv_grid.model.CASES`, applied with ``electrical.case_net`` and solved before writing.

    Returns:
        The written path.

    Raises:
        ValueError: Unknown case.
        LookupError: The variant or grid does not exist, or the grid has no stored net.
    """
    import pandapower as pp

    if case not in CASES:
        raise ValueError(f"case must be one of {', '.join(CASES)}, got {case!r}")
    output = Path(output)
    with runtime.database_client("log/log.txt") as dbc:
        dbc.ensure_grid_persistence_schema()
        row = dbc.variant_row(version_id, variant_id)
        if row is None:
            raise LookupError(f"MV variant {variant_id} of version {version_id} does not exist")
        params = MvParameters.from_snapshot(variant_id, row.get("mv_variant_comment") or "", row["mv_parameters"])
        _, grid = dbc.load_district_nodes(mv_grid_id)
        if grid["version_id"] != version_id or grid["mv_variant_id"] != variant_id:
            raise LookupError(f"MV grid {mv_grid_id} does not belong to variant {variant_id} of version {version_id}")
        net = dbc.read_mv_grid_net(mv_grid_id)
    if case != "load":
        net = runtime.module("electrical").case_net(net, case, params)
        try:
            pp.runpp(net, algorithm="nr", init="auto")
        except Exception as exc:   # the net is written without results
            logging.getLogger(__name__).warning(f"Case {case} of MV grid {mv_grid_id} did not converge: {exc}")
    output.parent.mkdir(parents=True, exist_ok=True)
    pp.to_json(net, str(output))
    return output
