"""Small database tasks the UI runs as jobs (``python -m pylovo_api.tasks …``).

pylovo's CLIs have no command for them. They run as job subprocesses (not in the web
server) so they take part in the one-writer rule of :class:`pylovo_api.jobs.JobManager` and
show up in the job log.

``clear-analysis --plz P --version V``
    Delete the analysis results of one PLZ and version (``plz_parameters`` and the
    ``clustering_parameters`` of its grids). ``pylovo-analyze`` skips a PLZ that already has
    ``plz_parameters``, so a re-analysis first clears them.
"""
from __future__ import annotations

import argparse
import sys


def clear_analysis(plz: int, version_id: str) -> tuple[int, int]:
    """Delete ``plz_parameters`` and ``clustering_parameters`` of one PLZ and version.

    Returns:
        Number of deleted ``plz_parameters`` and ``clustering_parameters`` rows.
    """
    from pylovo_api import db

    with db.cursor(readonly=False) as cur:
        cur.execute("""DELETE FROM pylovo.clustering_parameters WHERE grid_result_id IN (
                           SELECT grid_result_id FROM pylovo.grid_result WHERE version_id = %s AND plz = %s)""",
                    (version_id, plz))
        clustering = cur.rowcount
        cur.execute("DELETE FROM pylovo.plz_parameters WHERE version_id = %s AND plz = %s", (version_id, plz))
        return cur.rowcount, clustering


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pylovo_api.tasks")
    sub = parser.add_subparsers(dest="task", required=True)
    clear = sub.add_parser("clear-analysis", help="delete the analysis results of one PLZ and version")
    clear.add_argument("--plz", type=int, required=True)
    clear.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    if args.task == "clear-analysis":
        plz_rows, grid_rows = clear_analysis(args.plz, args.version)
        print(f"✓ Cleared the analysis of PLZ {args.plz}, version {args.version}: "
              f"{plz_rows} plz_parameters row(s), {grid_rows} clustering_parameters row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
