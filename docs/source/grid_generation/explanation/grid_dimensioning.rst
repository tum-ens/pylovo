Electrical Dimensioning
=======================

This page explains how PyLoVo converts building loads into transformer, feeder,
and service-cable dimensions. It first presents the core method and then lists
the configuration levers and additional validation information.

The network elements discussed here are:

.. code-block:: text

   transformer LV bus -- feeder sections -- connection point -- service cable -- building

``Feeder`` refers to the street-side network between the transformer and a
connection point. ``Service cable`` refers to the final connection from that
point to one building bus.

Method at a glance
------------------

#. Build residential, commercial, and public load components from the building
   data. Mixed-use buildings can contain more than one component.
#. Calculate coincident demand separately for each consumer category.
#. Select a transformer that can supply the coincident demand of its complete
   building cluster.
#. Construct a radial street-routed feeder topology.
#. Size every service cable for the local coincident demand of its own building,
   then upsize its conductor if needed to meet the service voltage-drop limit.
#. Flag service connections longer than 100 m for review without changing their size because of length alone.
#. Size every feeder section for the greatest coincident current on an edge in
   that section.
#. Apply the configured end-to-end feeder voltage-drop plausibility check and,
   where possible, increase feeder conductor size.
#. Run a power flow for the exported proportional validation snapshot and store
   its status and voltage-drop diagnostics.

The dimensioning calculation and the final power-flow assessment are separate.
The former selects equipment; the latter reports the electrical state of the
resulting network.

Coincident demand
-----------------

For one consumer category, PyLoVo calculates coincident active power as

.. math::

   P_{\mathrm{sim}} = P_{\mathrm{installed}}
   \left[f + (1-f)N^{-3/4}\right],

where ``f`` is the configured category-specific ``sim_factor`` and ``N`` is the
number of load units. For residential demand, ``N`` is the number of households.
Commercial and public components currently use their respective component
counts and coincidence factors.

When several categories are present, the calculation is performed separately
for each category and the resulting coincident powers are added. A residential
and commercial component in one mixed-use building therefore do not share one
combined ``N``.

Understanding ``N`` along a feeder
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

For every feeder edge, ``N`` and installed power include all relevant consumers
downstream of that edge. Near the transformer, an edge normally supplies many
buildings. Farther downstream, branches split and the downstream population
normally becomes smaller. Along one radial path, the downstream set is nested,
so ``N`` cannot increase when moving away from the transformer.

As ``N`` decreases, the coincidence factor in brackets increases: fewer
consumers receive less diversity. At the same time, the installed power behind
the edge normally decreases. The required current therefore usually decreases
towards the terminal branches, but not because the coincidence factor decreases;
it decreases because substantially less load remains downstream. A large
apartment or commercial building can still require more current than an upstream
segment serving several small buildings on another branch.

For residential demand, installed power is proportional to the household count,
so its coincident category load decreases monotonically as households are
removed from the downstream set. This monotonicity is not guaranteed for the
current commercial and public representation: every component counts as one
load unit although component peak powers can differ. Adding a small component
can then reduce the coincidence factor applied to a much larger existing power.
Consequently, a downstream feeder requirement normally decreases, but the
implementation deliberately calculates every edge instead of assuming that it
must. Within a uniform section, all edges use the greatest calculated current;
the cable can change only at the next hard-node boundary.

Design current
--------------

Active design power is converted into balanced three-phase current using

.. math::

   I = \frac{P}{\sqrt{3}\,V_n\,\cos\varphi}.

``VN`` provides :math:`V_n` and ``DEFAULT_POWER_FACTOR`` provides
:math:`\cos\varphi`. Nominal voltage has not been removed from the calculation:
power divided by voltage gives current, while the power factor converts active
power into the corresponding apparent-power requirement.

Service-cable sizing
--------------------

Each service cable is sized from the local coincident demand at its building
bus. For a residential building with five households, the residential
calculation therefore uses ``N = 5``. Unrelated buildings elsewhere in the grid
do not reduce this service design load.

PyLoVo initially selects the lowest-cost configured service cable with sufficient
ampacity. Additional parallel cables are used only if no single configured
cable can carry the required current.

It then calculates the approximate drop across every complete service cable at
the same building-local design load and compares it with
``MAX_SERVICE_DESIGN_VOLTAGE_DROP_PERCENT``. If necessary, the conductor is
upsized to the lowest-cost thermally feasible cable that satisfies this local
limit.

Voltage sizing never increases the ampacity-determined parallel count. If no
conductor at that parallel count can satisfy the limit, PyLoVo selects the
lowest-impedance available conductor and records that the limit remains unmet.

Independently, service connections longer than 100 m receive a
``service_length_review`` flag. This is a fixed data-quality review trigger: it
does not change cable selection, and a long connection with little load can
therefore remain on the ampacity-selected conductor.

Feeder ampacity sizing
----------------------

The finalized radial topology is represented as directed edges from the
transformer towards the connection points. For every edge, PyLoVo calculates
the coincident demand of all downstream consumers. This gives an
asset-specific design current for that edge.

Edges between hard nodes are grouped into uniform feeder sections. A hard node
is the transformer or a branching point. Every edge in one section receives the
same cable type and parallel count, based initially on the greatest design
current occurring in that section.

The initial feeder cable is the smallest configured conductor that satisfies
ampacity. The parallel count increases only when no single configured feeder
cable has sufficient ampacity.

End-to-end feeder voltage-drop check
------------------------------------

After ampacity sizing, complete paths from the transformer LV bus to every
load-bearing connection point are checked. The approximate three-phase drop of
one edge is based on

.. math::

   \Delta U_e = \sqrt{3}\,I_e L_e
   \frac{R\cos\varphi + X\sin\varphi}{n_{\mathrm{parallel}}}.

The edge drops are added along each path and compared with
``MAX_END_TO_END_FEEDER_VOLTAGE_DROP_PERCENT``. This setting applies only to
the street-side feeder. Transformer impedance and service-cable drop are not
part of this planning quantity.

Every edge uses the coincidence calculation for its own downstream consumer
set. Different edges can represent different hypothetical peak conditions, so
this is an asset-planning envelope rather than one common instant in time.

The exported validation snapshot is deliberately not included in conductor
selection. It is a diagnostic operating point, while the asset-specific
coincidence calculation is the planning basis.

If a path exceeds the configured limit, PyLoVo can increase conductor size
within the ampacity-required parallel count. It does not add voltage-driven
parallel circuits. If the largest available conductor cannot satisfy the limit,
the network is retained and the unresolved planning violation is recorded.
Changing topology or transformer placement would then be required to satisfy
the envelope, but those alternatives are not part of this cable-selection step.

The configured percentage should be interpreted as a modeling and plausibility
assumption, not automatically as a complete supply-voltage allowance. For
example, a 10 percent feeder threshold leaves no guaranteed margin for
transformer and service-cable voltage drop.

Validation snapshot and power flow
----------------------------------

The exported ``p_mw`` values form one transformer-coincident static operating
point. For every category :math:`c`, its transformer-coincident total is
distributed in proportion to the installed category power of component
:math:`i`:

.. math::

   P_{\mathrm{snapshot},i,c}
   = \frac{P_{\mathrm{installed},i,c}}
   {P_{\mathrm{installed,total},c}} P_{\mathrm{sim,total},c}
   = P_{\mathrm{installed},i,c} g(N_{\mathrm{total},c}).

Thus, all components of one category operate at the same fraction of installed
peak power in this synthetic snapshot. The allocation preserves every
transformer-level category total without using building-local coincidence as a
spatial weighting factor.

For any downstream subset, :math:`N_{\mathrm{subset},c}` cannot exceed the
transformer total. Since :math:`g(N)` decreases with :math:`N`, the snapshot
contribution of that category cannot exceed its asset-specific downstream
coincidence. The exported load records identify this basis as
``synthetic_transformer_coincident_proportional``.

This allocation is useful for a coherent generation-time power flow, but it is
not a replacement for asset-specific coincidence and it is not an hourly demand
model. Time-series studies should replace the static validation values with
their hourly profiles.

After network construction, PyLoVo solves this snapshot and classifies the
result with ``POWER_FLOW_VOLTAGE_LIMITS`` from ``config_analysis.yaml``. These
limits do not control numerical convergence and do not resize cables. Failed or
voltage-violating networks are retained with the corresponding status.

Configuration levers
--------------------

.. list-table:: Main dimensioning settings
   :header-rows: 1
   :widths: 31 39 30

   * - Setting
     - What it changes
     - What it does not change
   * - ``PEAK_LOAD_HOUSEHOLD``
     - Installed residential power before coincidence
     - Coincidence formula or cable impedances
   * - ``CONSUMER_CATEGORIES[].sim_factor``
     - Category-specific coincidence at every aggregation level
     - Street topology or cable ratings
   * - ``DEFAULT_POWER_FACTOR``
     - Design current, reactive power in the validation snapshot, and the
       approximate feeder and service voltage-drop calculations
     - Installed active power
   * - ``FEEDER_CABLES``
     - Available feeder ratings, impedances, sizes, and material costs
     - Service-cable choices
   * - ``CONSUMER_CONNECTION_CABLES``
     - Available service-cable ratings, impedances, sizes, and costs
     - Feeder-cable choices
   * - ``FEEDER_SPLIT_MAX_CURRENT_KA``
     - Branch construction and topology splitting
     - Final cable ampacity limit
   * - ``MIN_SHARED_PREFIX_LENGTH_M``
     - Whether later branches reuse an existing feeder prefix
     - Load magnitude or power-flow voltage limits
   * - ``MAX_SERVICE_DESIGN_VOLTAGE_DROP_PERCENT``
     - Service conductor upsizing after ampacity selection
     - Feeder sizing, voltage-driven parallel cables, topology, or power-flow classification
   * - ``MAX_END_TO_END_FEEDER_VOLTAGE_DROP_PERCENT``
     - Feeder conductor upsizing after ampacity selection
     - Service sizing, transformer/service drop, or power-flow classification
   * - ``POWER_FLOW_VOLTAGE_LIMITS``
     - Status assigned after a converged power flow
     - Topology, transformer selection, or cable sizing

Changing a generation parameter changes the meaning of the generated grid and
requires a new ``VERSION_ID``. The inline YAML comments contain the immediately
actionable defaults; this page provides their methodological context.

Stored diagnostics
------------------

The following fields in ``grid_result`` separate planning quantities from the
solved validation state:

``ampacity_max_feeder_voltage_drop_percent``
   Maximum asset-coincidence feeder path drop before voltage-driven conductor
   upsizing.

``selected_max_feeder_voltage_drop_percent``
   Maximum after the final feeder conductor selection.

``feeder_voltage_drop_limit_met``
   Whether the selected feeder design satisfies the configured planning limit.

``ampacity_max_service_voltage_drop_percent``
   Maximum local service design drop before voltage-driven conductor upsizing.

``selected_max_service_voltage_drop_percent``
   Maximum local service design drop after final conductor selection.

``service_voltage_drop_limit_met``
   Whether every selected service design satisfies the configured local limit.

``service_voltage_upgraded_count``
   Number of service conductors changed from their ampacity-only selection.

``long_service_connection_count``
   Number of service connections longer than the fixed 100 m review trigger.

``max_total_design_voltage_drop_percent``
   Maximum diagnostic sum of the selected asset-coincidence feeder path drop and
   selected local service design drop. Because the two asset peaks need not be
   simultaneous, this conservative quantity is reported but does not resize
   equipment.


``max_feeder_voltage_drop_pu``
   Solved voltage difference from the transformer LV bus to a service
   connection point in the validation snapshot.

``max_service_voltage_drop_pu``
   Solved voltage difference across a service cable.

``max_total_lv_voltage_drop_pu``
   Solved difference from the transformer LV bus to a consumer bus.

Feeder lines additionally store ``feeder_section_id``,
``feeder_sizing_basis``, ``ampacity_std_type``, and ``ampacity_parallel``. These
fields show which uniform section an edge belongs to, whether voltage planning
changed it, and what the ampacity-only design would have been.

Service lines store ``service_sizing_basis``, the before/after values
``service_ampacity_voltage_drop_percent`` and
``service_selected_voltage_drop_percent``, ``service_voltage_drop_limit_met``,
``service_length_review``, and ``total_design_voltage_drop_percent``.
``ampacity_std_type`` and ``ampacity_parallel`` retain the thermal-only service
selection for comparison.


Relationship to the published method
------------------------------------

The accompanying PyLoVo publication describes the methodological foundation:
geographic consumer and transformer assignment, category-specific coincidence,
radial street routing, transformer selection, and cable dimensioning. The
current implementation retains that foundation and additionally makes the
following distinctions explicit:

* residential and non-residential components can coexist at one connection;
* service cables use local building coincidence for ampacity and their separate
  voltage-drop limit rather than the allocated transformer validation snapshot;
* feeder edges are grouped into uniform sections between hard nodes;
* the end-to-end feeder plausibility pass is separated from ampacity sizing;
* the static validation operating point is allocated proportionally by
  installed category power and labeled explicitly; and
* planning provenance and solved feeder/service voltage-drop diagnostics are
  stored separately.

See :doc:`../../further_reading` for the publication reference.
