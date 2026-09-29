Electrical backends
===================

pylovo separates the grid construction from the electrical simulation software. The
:class:`~pylovo.cable_installer.CableInstaller` describes every element with a component
specification (:class:`~pylovo.electrical_backend.core.specs.BusSpec`,
:class:`~pylovo.electrical_backend.core.specs.TransformerSpec`,
:class:`~pylovo.electrical_backend.core.specs.LineSpec`,
:class:`~pylovo.electrical_backend.core.specs.LoadSpec`,
:class:`~pylovo.electrical_backend.core.specs.ExtGridSpec`); a backend implementing
:class:`~pylovo.electrical_backend.core.backend_base.IElectricalBackend` translates them into its
own model, solves the power flow and exports the grid.

``ELECTRICAL_BACKEND`` in ``config_generation.yaml`` selects the backend; backends are created by
:func:`~pylovo.electrical_backend.factory.create_backend` and new ones can be registered with
:func:`~pylovo.electrical_backend.factory.register_backend`.

pandapower (default)
--------------------

:class:`~pylovo.electrical_backend.pandapower.backend.PandapowerBackend` builds one
``pandapowerNet`` per grid:

.. list-table::
   :header-rows: 1
   :widths: 22 78
   :class: fixed-table

   * - Element
     - Model
   * - Buses
     - ``MVbus 1`` (20 kV), ``LVbus 1`` (``VN``, the station busbar, which is also the
       transformer's own street vertex), ``Connection Nodebus <vertex>`` for the other street
       vertices, ``Consumer Nodebus <vertex>`` for buildings; ``zone`` of a consumer bus is its load
       category (``Mixed`` for several). Coordinates are WGS84 in the ``geo`` column.
   * - External grid
     - At ``MVbus 1``; ``vm_pu`` is set so that ``LVbus 1`` is at ``LV_REFERENCE_VOLTAGE_PU`` at the
       validation operating point (1.0 without a reference).
   * - Transformer
     - 20/0.4 kV pandapower standard types. 100, 160, 250, 400 and 630 kVA are single units;
       500, 800 and 1260 kVA are two parallel units of half the rating; other ratings become
       parallel 630 kVA units. pandapower has no standard types below 0.25 MVA, so 100 and 160 kVA
       reuse the 0.25 MVA data with the rating and no-load losses scaled. The off-load tap (HV side,
       ±2 steps of 2.5 %) stays neutral. The OpenDSS backend does not support the reference voltage
       and keeps the MV side at 1.0 p.u.
   * - Lines
     - Standard types registered from the cable catalogue (``r``, ``x``, ``max_i_ka``; capacitance
       0). Feeder lines follow the street routes; service lines are straight connections. Extra
       columns store the sizing provenance (:ref:`stored-diagnostics`).
   * - Loads
     - One load per consumer and category (``Load <vertex> <category>``): ``p_mw`` is the
       validation snapshot, ``q_mvar`` follows ``DEFAULT_POWER_FACTOR``, ``max_p_mw`` the installed
       peak, ``service_design_p_mw`` the building-local design load; plus ``category``,
       ``load_units``, ``consumer_vertex`` and ``operating_point_basis``.

The power flow is ``pandapower.runpp(net, algorithm="nr", init="auto")``. The net is stored as
pandapower JSON in ``grid_result.grid`` and element by element in the ``pandapower_*`` tables.

Buses, loads and lines are created with pandapower's batch functions, one call per table and grid
(:meth:`~pylovo.electrical_backend.pandapower.backend.PandapowerBackend.batch`). pylovo's extra
columns of ``line`` and ``load`` have the same dtype in every stored net
(``LINE_ATTRIBUTE_DTYPES``, ``LOAD_ATTRIBUTE_DTYPES`` in
:mod:`pylovo.electrical_backend.pandapower.backend`): numbers are ``float64`` with NaN where they do
not apply, text and flags are objects with ``None``. Before, the first line of a grid decided the
dtype, so in grids generated earlier whose first line was a service line
``feeder_section_id`` is an object column and the ``service_*`` percentages are ``float64``.

OpenDSS (work in progress)
--------------------------

:class:`~pylovo.electrical_backend.opendss.backend.OpenDSSBackend` implements the same interface on
top of OpenDSS via ``altdss``. It is not complete: the package ``altdss`` is not part of the
dependencies, and the persistence of results in the ``pandapower_*`` tables and the voltage-drop
diagnostics are only available with the pandapower backend. Use ``ELECTRICAL_BACKEND: pandapower``
for production runs.
