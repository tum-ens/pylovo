"""Supply areas in one piece.

A grid whose buildings lie on both sides of the buildings of another grid is not realistic: the
feeders of a station run along the streets and pass the buildings they supply. Two rules of the
building clusters keep the areas in one piece:

* an existing station takes only the buildings for which it is the nearest station
  (``GridGenerator.position_brownfield_transformers``), so its area is connected along the streets. Only a small
  greenfield cluster that is left over may still join the next station in reach, if the joined area is in one piece
  (``GridGenerator.merge_small_greenfield_into_brownfield``);
* a greenfield area must form one piece among the buildings that other grids supply already
  (:func:`is_contiguous`). The buildings left over for greenfield stations lie in the gaps between the
  existing areas, and the greenfield clustering (average linkage on street distances) would otherwise
  join gaps on both sides of an existing station's area into one grid. An area in several pieces is
  split again like a too large one, and the merges keep the areas in one piece.

:func:`snapshot` records in the version snapshot where the two rules act.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd
import scipy.sparse
from scipy.sparse.csgraph import connected_components
from scipy.spatial import Delaunay, QhullError


#: Building centroids closer than this (m) count as one position: Qhull drops a point that close to another one at
#: projected coordinates, and such a pair is one place for the question whether an area is in one piece.
POSITION_RESOLUTION_M = 0.1


def is_contiguous(member_xy: np.ndarray, blocker_xy: np.ndarray) -> bool:
    """Return whether the member points form one piece among the blocker points.

    Members and blockers are joined by their Delaunay triangulation, which links neighbouring
    buildings whatever their distance. The members form one piece if the edges between two members
    connect all of them: blockers between two groups of members separate them, empty land does not.
    Positions are snapped to :data:`POSITION_RESOLUTION_M`; a member at the position of a blocker stays a
    member. If all positions lie on one line, the members must follow each other along it.

    Args:
        member_xy: ``(n, 2)`` coordinates of the buildings of the area.
        blocker_xy: ``(m, 2)`` coordinates of the buildings of other grids.

    Returns:
        True also for fewer than two members and without blockers.
    """
    member_xy = np.asarray(member_xy, dtype=float).reshape(-1, 2)
    blocker_xy = np.asarray(blocker_xy, dtype=float).reshape(-1, 2)
    if len(member_xy) < 2 or len(blocker_xy) == 0:
        return True
    snapped = np.round(np.vstack([member_xy, blocker_xy]) / POSITION_RESOLUTION_M)
    points, index = np.unique(snapped, axis=0, return_inverse=True)
    index = np.asarray(index).ravel()
    is_member = np.zeros(len(points), dtype=bool)
    is_member[index[:len(member_xy)]] = True
    members = np.flatnonzero(is_member)
    if len(members) < 2:
        return True
    # centred: at projected coordinates (10^5 to 10^6 m) Qhull loses the precision to tell close points apart
    points = (points - points.mean(axis=0)) * POSITION_RESOLUTION_M
    try:
        triangulation = Delaunay(points)
    except (QhullError, ValueError):
        return _contiguous_on_a_line(points, is_member)
    simplices = triangulation.simplices
    edges = np.vstack([simplices[:, [0, 1]], simplices[:, [1, 2]], simplices[:, [0, 2]],
                       # a point Qhull still left out (coplanar) is linked to its nearest vertex
                       triangulation.coplanar[:, [0, 2]].reshape(-1, 2)])
    edges = edges[is_member[edges[:, 0]] & is_member[edges[:, 1]]]
    local = np.full(len(points), -1)
    local[members] = np.arange(len(members))
    graph = scipy.sparse.coo_matrix((np.ones(len(edges)), (local[edges[:, 0]], local[edges[:, 1]])),
                                    shape=(len(members), len(members)))
    return connected_components(graph, directed=False)[0] == 1


def _contiguous_on_a_line(points: np.ndarray, is_member: np.ndarray) -> bool:
    """All points on one line (no triangulation): the members must follow each other without a blocker between."""
    far = points[np.argmax(np.linalg.norm(points - points[0], axis=1))]
    run = np.flatnonzero(is_member[np.argsort((points - points[0]) @ (far - points[0]))])
    return bool(run[-1] - run[0] + 1 == len(run))


class AreaContiguity:
    """The check of one kcid: do the buildings at some planning nodes form one piece?

    Built from the consumers of the kcid (``ClusteringMixin.get_consumer_positions``): the buildings
    without a grid are the candidates, those of a grid the blockers. Without blockers (a kcid without
    existing stations, before any grid is formed) every area is contiguous and nothing changes.
    """

    def __init__(self, positions: pd.DataFrame):
        """
        Args:
            positions: One row per consumer building with ``point`` (planning node), ``bcid`` (NaN without
                a grid), ``x`` and ``y``.
        """
        free = positions[positions["bcid"].isna()]
        self._xy_by_point = {int(point): group[["x", "y"]].to_numpy(dtype=float)
                             for point, group in free.groupby("point")}
        self._blockers = positions.loc[positions["bcid"].notna(), ["x", "y"]].to_numpy(dtype=float)
        self._answers: dict[frozenset[int], bool] = {}   # the merge asks again for the same areas

    def __call__(self, points: Iterable[int]) -> bool:
        """Return whether the buildings at ``points`` form one piece among the buildings of other grids."""
        if len(self._blockers) == 0:
            return True
        key = frozenset(int(p) for p in points)
        if key not in self._answers:
            xy = [self._xy_by_point[p] for p in key if p in self._xy_by_point]
            self._answers[key] = is_contiguous(np.vstack(xy) if xy else np.zeros((0, 2)), self._blockers)
        return self._answers[key]


def snapshot(existing_stations: bool, max_voltage_repairs: int) -> dict[str, bool]:
    """Where the two rules act, for the version snapshot (``transformer_placement``).

    The nearest-station rule acts only with existing stations. The greenfield check acts only where
    other grids exist when a greenfield area is formed: next to existing stations and in the voltage
    repair. Versions stored before the rules recorded ``False`` (or nothing), so pylovo does not extend
    a version with existing stations or voltage repair with grids of these rules; a greenfield version
    without repair, whose grids the rules do not change, can still be extended.

    Args:
        existing_stations: Whether the run uses existing stations (any of ``USE_DSO_TRANSFORMER_POSITIONS``,
            ``USE_OPEN_TRANSFORMER_POSITIONS`` and ``USE_MANUAL_TRANSFORMER_POSITIONS``).
        max_voltage_repairs: ``MAX_VOLTAGE_REPAIRS``.
    """
    return {
        "brownfield_nearest_only": bool(existing_stations),
        "greenfield_contiguous_areas": bool(existing_stations) or (max_voltage_repairs or 0) > 0,
    }
