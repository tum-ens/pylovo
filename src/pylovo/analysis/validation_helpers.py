"""Source-side validation export helpers."""
from pathlib import Path

import pandapower as pp
from tqdm import tqdm


def export_synthetic_grids_to_excel(
    output_dir: Path | str,
    plz: int | None = None,
    kcid: int | None = None,
    bcid: int | None = None,
) -> list[Path]:
    """Export synthetic grids from the database as Excel files using ``pp.to_excel``.

    Fetches one specific grid when *plz*, *kcid*, and *bcid* are all provided.
    Fetches every grid stored for the current ``VERSION_ID`` when all three are
    ``None``.  Mixed partial arguments raise a ``ValueError``.

    Args:
        output_dir: Directory where the ``.xlsx`` files will be written.
        plz: Postal code.  Must be given together with *kcid* and *bcid*.
        kcid: K-means cluster ID.  Must be given together with *plz* and *bcid*.
        bcid: Building cluster ID.  Must be given together with *plz* and *kcid*.

    Returns:
        List of :class:`~pathlib.Path` objects for every file that was written.

    Raises:
        ValueError: If only some of *plz*/*kcid*/*bcid* are provided, or if a
            requested grid does not exist in the database.
    """
    from pylovo.config_loader import VERSION_ID
    from pylovo.database.database_client import DatabaseClient

    identifiers = (plz, kcid, bcid)
    n_given = sum(v is not None for v in identifiers)
    if n_given not in (0, 3):
        raise ValueError(
            "Provide either all three of plz/kcid/bcid (single grid) "
            "or none of them (all grids)."
        )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []

    with DatabaseClient() as dbc:
        if n_given == 3:
            rows = [(int(plz), int(kcid), int(bcid))]
        else:
            dbc.cur.execute(
                f"SELECT plz, kcid, bcid FROM pylovo.grid_result "
                "WHERE version_id = %s AND grid IS NOT NULL",
                (VERSION_ID,),
            )
            rows = dbc.cur.fetchall()

        for row_plz, row_kcid, row_bcid in tqdm(rows, desc="Exporting grids"):
            try:
                net = dbc.read_net_db(int(row_plz), int(row_kcid), int(row_bcid))
            except ValueError:
                continue

            xlsx_path = output_dir / f"grid_{row_plz}_{row_kcid}_{row_bcid}.xlsx"
            pp.to_excel(net, str(xlsx_path))
            written.append(xlsx_path)

    return written


__all__ = ["export_synthetic_grids_to_excel"]
