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

``ELECTRICAL_BACKEND`` in ``config_generation_lv.yaml`` selects the backend; backends are created by
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
       ±2 steps of 2.5 %) stays neutral.
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
not apply, except the three voltage-drop percentages ``service_ampacity_voltage_drop_percent``,
``service_selected_voltage_drop_percent`` and ``total_design_voltage_drop_percent``, which are objects
with ``None`` where they do not apply (convert them with ``pandas.to_numeric`` before you compute with
them). Text and flags are objects with ``None``. Before, the first line of a grid decided the dtype, so
in grids generated earlier whose first line was a service line ``feeder_section_id`` is an object column
and the ``service_*`` percentages are ``float64``.

OpenDSS
-------

:class:`~pylovo.electrical_backend.opendss.backend.OpenDSSBackend` builds the same grid in OpenDSS via
``altdss`` (DSS-Extensions). Install it with ``uv sync --extra opendss``. Its LV power flow matches
pandapower's: on the same grid with the LV reference voltage, the LV bus voltages agree within
1e-4 p.u. and the line loadings within 0.1 percentage points (``tests/test_opendss_backend.py``).

.. list-table::
   :header-rows: 1
   :widths: 22 78
   :class: fixed-table

   * - Element
     - Model, and how it differs from pandapower
   * - Buses
     - pylovo's names with blanks and dots replaced by ``_``, in lower case (``Consumer Nodebus 21``
       becomes ``consumer_nodebus_21``), because the OpenDSS parser splits names at blanks and OpenDSS
       lowercases every name. A duplicate element name gets a suffix (``line_to_23_2``). No coordinates.
   * - External grid
     - The circuit's source at ``MVbus 1`` with a short-circuit power of 1000 MVA (pandapower: an ideal
       slack). Its ``pu`` is set so that ``LVbus 1`` is at ``LV_REFERENCE_VOLTAGE_PU``, as in pandapower.
   * - Transformer
     - One transformer with the summed rating of the parallel units, 4 % reactance, 1 % resistance and no
       no-load losses (pandapower: the standard type's ``vk_percent``, ``vkr_percent``, ``pfe_kw``,
       ``i0_percent``).
   * - Lines
     - One line code per cable type and number of parallel cables: impedances divided, ampacity
       multiplied. Capacitance 0 as in pandapower. An unknown cable type falls back to the strongest
       registered cable of its family with a warning (pandapower raises).
   * - Loads
     - Three-phase constant power between 0.7 and 1.3 p.u. (OpenDSS's default switches to constant
       impedance below 0.95 p.u.; pandapower keeps constant power).

With the LV busbar at the reference voltage in both backends, the transformer and the source change the
MV-side voltage and the transformer losses, not the LV voltages and line loadings.

``grid_result.grid`` stores the AltDSS JSON of the circuit with the source voltage of the check, and
``power_flow_status`` is classified as for pandapower. ``DSS.ActiveCircuit.FromJSON()`` of
``dss-python`` loads the JSON back, and solving it reproduces the results. The JSON has no results, no
line routes and none of pylovo's line and load attributes. An OpenDSS grid also has no rows in the
``pandapower_*`` tables, no voltage-drop diagnostics (``max_feeder_voltage_drop_pu`` and the two others
stay empty) and ``transformer_description`` ``N/A``.

pylovo's tools after the generation read pandapower grids. ``pylovo-analyze``, ``pylovo-export``, the
plots and the power flow of the HTTP API stop with a message that the grid comes from the OpenDSS
backend. Load editing and the MV stage refuse OpenDSS grids. The viewer, the grid maps of the HTTP API and
GridExpand read the ``pandapower_*`` tables and show nothing for them.
