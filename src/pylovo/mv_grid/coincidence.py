"""Coincident loads of sets of MV nodes with pylovo's category law (pure numpy).

pylovo sizes every LV station with the coincident peak of its buildings (``utils.category_simultaneous_load``):
per consumer category ``c`` the installed peaks ``I_c`` and the load units ``N_c`` (households of residential
loads, one unit per non-residential load) are added, and

``P = sum over c of I_c * (g_c + (1 - g_c) * N_c ** (-3/4))``  (a term is 0 where ``I_c <= 0`` or ``N_c <= 0``)

with the sim factor ``g_c`` of the category. The law works on sums, so the coincident load of any set of MV nodes
(a ring section, a half-ring, a feeder, a whole district) follows from the summed per-category statistics of its
nodes; the peaks of the stations are never added. MV customers carry a fixed load (``p_fixed_kw`` = ``g_c`` times
their installed load), which is added to every set as it is.

Array conventions: the last axis of ``installed`` and ``units`` runs over
:data:`~pylovo.mv_grid.model.CATEGORIES` (Residential, Commercial, Public, Unknown); ``g`` is the (4,) array of the
sim factors in the same order (:meth:`~pylovo.mv_grid.parameters.LvVersionParameters.g`). Loads are in kW.
Missing (NaN) or negative installed loads and units count as 0, like the components pylovo never creates for
them; a missing fixed load counts as 0.

* :func:`category_kw`, :func:`coincident_kw`, :func:`pooled_kw`: the law per category, per set and for all rows
  pooled.
* :func:`segment_loads`: the loads of all contiguous position ranges of a ring (prefix sums), for the ring
  evaluator.
* :func:`feeder_scaling`, :func:`allocate`: node loads whose sum per feeder is the coincident load of the feeder
  (the MV analogue of ``utils.allocate_consumer_simultaneous_loads``), for the power flows.
* :func:`node_stats`: the statistics arrays of a node table with :data:`~pylovo.mv_grid.model.NODE_COLUMNS`.
"""
from __future__ import annotations

import functools
import math

import numpy as np
import pandas as pd

from pylovo.mv_grid.model import CATEGORIES

#: Exponent of the load units in the category law (pylovo: ``load_count ** (-3 / 4)``).
UNITS_EXPONENT: float = -3 / 4

#: Columns of the installed load (kW) per category in a node table, in CATEGORIES order.
INSTALLED_COLUMNS: tuple[str, ...] = ("inst_res", "inst_com", "inst_pub", "inst_unk")
#: Columns of the load units per category in a node table, in CATEGORIES order.
UNITS_COLUMNS: tuple[str, ...] = ("units_res", "units_com", "units_pub", "units_unk")
#: Column of the fixed load (kW) of MV customers in a node table.
FIXED_COLUMN: str = "p_fixed_kw"

_N_CATEGORIES = len(CATEGORIES)
# pylovo adds the category loads in the order of ``groupby("category")``, i.e. by name; the same order here keeps
# the sums of the categories bit-identical to utils.simultaneous_peak_load for identical category terms.
_SUM_ORDER: tuple[int, ...] = tuple(sorted(range(_N_CATEGORIES), key=lambda i: CATEGORIES[i]))


def _stats(installed, units) -> tuple[np.ndarray, np.ndarray]:
    """``installed`` and ``units`` as float arrays of the same shape with the categories on the last axis."""
    inst = np.asarray(installed, dtype=float)
    cnt = np.asarray(units, dtype=float)
    if inst.shape != cnt.shape or inst.ndim == 0 or inst.shape[-1] != _N_CATEGORIES:
        raise ValueError(f"installed and units must have the same shape (..., {_N_CATEGORIES}) with the categories "
                         f"{', '.join(CATEGORIES)} on the last axis, got {inst.shape} and {cnt.shape}")
    return inst, cnt


def _factors(g) -> np.ndarray:
    factors = np.asarray(g, dtype=float)
    if factors.shape != (_N_CATEGORIES,):
        raise ValueError(f"g must hold one sim factor per category ({', '.join(CATEGORIES)}), got shape "
                         f"{factors.shape}")
    return factors


def _fixed(fixed_kw, n: int) -> np.ndarray:
    fixed = np.asarray(fixed_kw, dtype=float)
    if fixed.shape != (n,):
        raise ValueError(f"fixed_kw must have one value per row ({n},), got shape {fixed.shape}")
    return np.where(np.isnan(fixed), 0.0, fixed)


def _clean(values: np.ndarray) -> np.ndarray:
    """``values`` with NaN and negative entries set to 0 (they are no load components)."""
    return np.where(values > 0, values, 0.0)


def _law(inst: np.ndarray, cnt: np.ndarray, factors: np.ndarray) -> np.ndarray:
    """The category terms without input checks (see :func:`category_kw`)."""
    valid = (inst > 0) & (cnt > 0)
    safe_units = np.where(valid, cnt, 1.0)
    return np.where(valid, inst * (factors + (1.0 - factors) * safe_units ** UNITS_EXPONENT), 0.0)


@functools.lru_cache(maxsize=256)
def _below_diagonal(k: int) -> np.ndarray:
    mask = np.tril(np.ones((k, k), dtype=bool), -1)
    mask.flags.writeable = False
    return mask


def _sum_categories(kw: np.ndarray) -> np.ndarray:
    total = kw[..., _SUM_ORDER[0]]
    for index in _SUM_ORDER[1:]:
        total = total + kw[..., index]
    return total


def category_kw(installed, units, g) -> np.ndarray:
    """Coincident load per category: ``I_c * (g_c + (1 - g_c) * N_c ** (-3/4))``.

    Args:
        installed: Installed load in kW, shape (..., 4) in CATEGORIES order.
        units: Load units, same shape.
        g: Sim factors, shape (4,).

    Returns:
        Array of the shape of ``installed``; a term is 0 where its installed load or its units are not positive
        (or NaN).

    Raises:
        ValueError: If the shapes do not fit.
    """
    inst, cnt = _stats(installed, units)
    return _law(inst, cnt, _factors(g))


def coincident_kw(installed, units, g) -> np.ndarray:
    """Coincident load of sets of loads from their summed statistics (pylovo's station design law).

    For the statistics of one LV grid this is its station peak, equal to the pooled
    ``utils.simultaneous_peak_load`` of its buildings; for the summed statistics of several nodes it is their
    coincident load.

    Args:
        installed: Summed installed load per category in kW, shape (..., 4).
        units: Summed load units per category, same shape.
        g: Sim factors, shape (4,).

    Returns:
        Coincident load in kW, shape ``installed.shape[:-1]`` (a NumPy scalar for one set).

    Raises:
        ValueError: If the shapes do not fit.
    """
    return _sum_categories(category_kw(installed, units, g))


def pooled_kw(installed, units, fixed_kw, g) -> float:
    """Coincident load of all rows together: the law on the summed statistics plus the summed fixed loads.

    This is the load of a whole feeder, stub tree or district (never the sum of the row loads).

    Args:
        installed: Installed load per row and category in kW, shape (n, 4).
        units: Load units per row and category, shape (n, 4).
        fixed_kw: Fixed load per row in kW (MV customers), shape (n,).
        g: Sim factors, shape (4,).

    Returns:
        Coincident load in kW (0.0 without rows).

    Raises:
        ValueError: If the shapes do not fit.
    """
    inst, cnt = _stats(installed, units)
    if inst.ndim != 2:
        raise ValueError(f"installed and units must have the shape (n, {_N_CATEGORIES}), got {inst.shape}")
    fixed = _fixed(fixed_kw, inst.shape[0])
    return float(coincident_kw(_clean(inst).sum(axis=0), _clean(cnt).sum(axis=0), g) + fixed.sum())


def segment_loads(installed, units, fixed_kw, g) -> np.ndarray:
    """Coincident loads of all contiguous position ranges of a sequence (a ring or a radial feeder).

    ``P[a, b]`` is the load of the positions ``a`` to ``b`` (0-based, both included): the category law on their
    summed statistics plus their summed fixed loads, computed with prefix sums (``O(k * k * 4)``). The law is not
    monotone in the non-residential terms, so callers use every range they need and never infer one range from
    another.

    Args:
        installed: Installed load per position and category in kW, shape (k, 4).
        units: Load units per position and category, shape (k, 4).
        fixed_kw: Fixed load per position in kW (MV customers, stub subtrees at their joint), shape (k,).
        g: Sim factors, shape (4,).

    Returns:
        (k, k) array in kW; NaN below the diagonal (``b < a``).

    Raises:
        ValueError: If the shapes do not fit.
    """
    inst, cnt = _stats(installed, units)
    if inst.ndim != 2:
        raise ValueError(f"installed and units must have the shape (k, {_N_CATEGORIES}), got {inst.shape}")
    k = inst.shape[0]
    fixed = _fixed(fixed_kw, k)
    zero = np.zeros((1, _N_CATEGORIES))
    prefix_inst = np.concatenate((zero, np.cumsum(_clean(inst), axis=0)))
    prefix_units = np.concatenate((zero, np.cumsum(_clean(cnt), axis=0)))
    prefix_fixed = np.concatenate(([0.0], np.cumsum(fixed)))
    # [a, b] = prefix[b + 1] - prefix[a]; ranges with b < a are negative and masked below
    range_inst = prefix_inst[None, 1:, :] - prefix_inst[:-1, None, :]
    range_units = prefix_units[None, 1:, :] - prefix_units[:-1, None, :]
    loads = _sum_categories(_law(range_inst, range_units, _factors(g)))
    loads += prefix_fixed[None, 1:] - prefix_fixed[:-1, None]
    loads[_below_diagonal(k)] = np.nan
    return loads


def feeder_scaling(installed_f, units_f, g) -> np.ndarray:
    """Coincident load per installed kW of each category of a feeder set: ``u_c = P_c(F) / I_c(F)``.

    Every node of the feeder gets ``sum over c of u_c * I_node,c``, so the node loads of the feeder add up to its
    coincident load (as ``utils.allocate_consumer_simultaneous_loads`` does for the loads of one LV grid).

    Args:
        installed_f: Summed installed load of the feeder per category in kW, shape (4,) or (..., 4) for several
            feeders.
        units_f: Summed load units of the feeder per category, same shape.
        g: Sim factors, shape (4,).

    Returns:
        Array of the shape of ``installed_f``; 0 for a category without installed load or units.

    Raises:
        ValueError: If the shapes do not fit.
    """
    inst, _ = _stats(installed_f, units_f)
    kw = category_kw(installed_f, units_f, g)
    return np.divide(kw, inst, out=np.zeros_like(kw), where=kw > 0)


def allocate(installed, units, fixed_kw, feeder_of, g) -> np.ndarray:
    """Node loads (kW) whose sum over each feeder set equals the coincident load of the set.

    Per set ``F`` and category ``c`` the scaling ``u_c(F)`` of :func:`feeder_scaling` is applied to the installed
    load of each node; MV customers keep their fixed load. Hence the loads of the nodes of ``F`` add up to
    ``coincident_kw(I(F), N(F)) + sum of the fixed loads of F``.

    Args:
        installed: Installed load per node and category in kW, shape (n, 4).
        units: Load units per node and category, shape (n, 4).
        fixed_kw: Fixed load per node in kW, shape (n,).
        feeder_of: Integer label of the set of each node, shape (n,); for example the half-ring of each node in
            the normal case or the ring in the (n-1) cases. A node with a negative label is a set of its own (it
            gets its own coincident load).
        g: Sim factors, shape (4,).

    Returns:
        Load per node in kW, shape (n,).

    Raises:
        ValueError: If the shapes do not fit or a label is not an integer.
    """
    inst, cnt = _stats(installed, units)
    if inst.ndim != 2:
        raise ValueError(f"installed and units must have the shape (n, {_N_CATEGORIES}), got {inst.shape}")
    n = inst.shape[0]
    fixed = _fixed(fixed_kw, n)
    labels = np.asarray(feeder_of)
    if labels.shape != (n,):
        raise ValueError(f"feeder_of must have one label per node ({n},), got shape {labels.shape}")
    if labels.dtype.kind not in "iub":
        try:
            as_float = labels.astype(float)
        except (TypeError, ValueError):
            as_float = np.array([np.nan])
        if not (np.isfinite(as_float).all() and (as_float == np.round(as_float)).all()):
            raise ValueError("feeder_of must hold integer labels")
    labels = labels.astype(np.int64)
    inst, cnt = _clean(inst), _clean(cnt)
    own = labels < 0
    if own.any():   # every node with a negative label is its own set
        labels = labels.copy()
        start = int(labels.max()) + 1 if (~own).any() else 0
        labels[own] = start + np.arange(int(own.sum()))
    sets, index = np.unique(labels, return_inverse=True)
    inst_f = np.stack([np.bincount(index, weights=inst[:, c], minlength=len(sets)) for c in range(_N_CATEGORIES)],
                      axis=1)
    units_f = np.stack([np.bincount(index, weights=cnt[:, c], minlength=len(sets)) for c in range(_N_CATEGORIES)],
                       axis=1)
    scaling = feeder_scaling(inst_f, units_f, g)
    return _sum_categories(scaling[index] * inst) + fixed


def node_stats(nodes: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The statistics arrays of a node table (columns :data:`INSTALLED_COLUMNS`, :data:`UNITS_COLUMNS` and
    :data:`FIXED_COLUMN`, as in :data:`~pylovo.mv_grid.model.NODE_COLUMNS`).

    Args:
        nodes: Node table, one row per node (for example ``DistrictInput.nodes``).

    Returns:
        ``(installed (n, 4), units (n, 4), fixed_kw (n,))`` as float arrays in row order; missing values are 0.

    Raises:
        KeyError: If a column is missing.
    """
    installed = nodes.loc[:, list(INSTALLED_COLUMNS)].to_numpy(dtype=float, na_value=0.0)
    units = nodes.loc[:, list(UNITS_COLUMNS)].to_numpy(dtype=float, na_value=0.0)
    fixed = nodes[FIXED_COLUMN].to_numpy(dtype=float, na_value=0.0)
    return installed, units, fixed


def tan_phi(cos_phi: float) -> float:
    """``tan(arccos(cos_phi))``: reactive power per active power of a load at power factor ``cos_phi``.

    Raises:
        ValueError: If ``cos_phi`` is not in (0, 1].
    """
    if not 0.0 < cos_phi <= 1.0:
        raise ValueError(f"a power factor must be in (0, 1], got {cos_phi!r}")
    return math.tan(math.acos(cos_phi))
