Input data
==========

A grid is built from three inputs per postcode area: **buildings** (geometry, use, households,
floor areas), the **street network** and the **postcode polygon**. Transformer positions are an
optional fourth input (:doc:`transformer_data`). pylovo reads the first three from an InfDB. Version 1.2
and earlier also read them from files (shapefiles and an osm2po street dump); that mode was removed, as an
InfDB can be prepared for any region.

.. figure:: /images/generation/step1_inputs.png
   :alt: Buildings coloured by use, street network and transformer candidates of PLZ 85653
   :width: 90%

   Input data of the demo region PLZ 85653 (Aying): buildings by use, streets and transformer
   candidates. Demo extract derived from OpenStreetMap, © OpenStreetMap contributors, ODbL;
   figure made with ``docs/scripts/plot_generation_steps.py``.

InfDB input
-----------

`InfDB <https://github.com/tum-ens/InfDB>`_ prepares the geographically referenced inputs before
pylovo generates grids. It consolidates LoD2 building geometry and attributes, allocates
Zensus 2022 information to buildings, classifies building use, and makes an address-aware street
graph. This is a separate preparation step: first set up InfDB and run its pylovo preprocessing
for the region, then run ``pylovo-setup`` against the **same database**. The source schemas remain
owned by InfDB and the results go into a new ``pylovo`` schema. See :doc:`database_setup`, the
`InfDB documentation <https://tum-ens.github.io/InfDB/usage/>`_ and the
`InfDB paper <https://doi.org/10.21105/joss.10458>`_ and the
`pylovo input-data paper <https://doi.org/10.30420/566656008>`_.

.. _infdb-tables:

Required InfDB tables
~~~~~~~~~~~~~~~~~~~~~

pylovo reads these tables from the database configured in ``.env``:

.. list-table::
   :header-rows: 1
   :widths: 36 64
   :class: fixed-table

   * - Table
     - Purpose
   * - ``<INFDB_SOURCE_SCHEMA>.buildings``
     - Building geometry, use, households, floor areas, postcode and address attributes. The
       postcode column selects buildings for a run.
   * - ``<INFDB_SOURCE_SCHEMA>.classify_building_use(text)``
     - Classifies mixed-use building functions as Commercial or Public.
   * - ``<INFDB_SOURCE_SCHEMA>.ways_per_connection``
     - Prepared street segments and their postcode assignment.
   * - ``<INFDB_SOURCE_SCHEMA>.connection_lines``
     - Lines connecting building centroids to the street graph.
   * - ``<INFDB_OPENDATA_SCHEMA>.postcodes_germany``
     - Postcode polygons and regional attributes.

The default source schema is ``basedata`` and the default open-data schema is ``opendata``.
Buildings, the function ``classify_building_use`` and the street tables are read from
``INFDB_SOURCE_SCHEMA``. All source geometries need a defined SRID; pylovo transforms them to
``TARGET_EPSG``.

The MV stage reads further inputs: the regional street graph
``<INFDB_SOURCE_SCHEMA>.ways_per_junction``, the OSM power and land-use layers of
``INFDB_OSM_SCHEMA`` and, optionally, the eGon-data HV/MV substations
(:ref:`mv-prerequisites`).

Buildings
---------

Selection
~~~~~~~~~

From ``<INFDB_SOURCE_SCHEMA>.buildings`` pylovo takes the buildings of the postcode whose ``building_use`` is
``Residential``, ``Commercial``, ``Public``, ``Mixed`` or ``Unknown``. Further rules:

* ``Auxiliary`` buildings are no consumers: InfDB gives this use to garages, sheds, barns and other outbuildings
  without a load of their own (by function code, address and geometry; the column ``auxiliary_rule`` names the
  rule).
* Buildings with function code ``31001_2523`` (transformer stations in LoD2, in InfDB ``Auxiliary``) are not
  consumers; with open or manual transformer positions they are added to the transformer candidates
  (:doc:`transformer_data`).
* ``Unknown`` buildings have no non-residential load (``peak_load_per_m2`` 0 in ``CONSUMER_CATEGORIES``); one
  without residential floor area is left out as unloaded.
* Leisure buildings (function codes ``31001_1310`` to ``31001_1312``, weekend and holiday houses) are
  ``Residential`` with one household and no residents in InfDB: each gets the load of one household.
* ``EXCLUDE_BUILDINGS_WITHOUT_ADDRESS: True`` leaves out buildings without street or house number. The shipped
  configuration sets it ``True``, as the central InfDB has no class ``Auxiliary`` yet (no column
  ``auxiliary_rule``): its garages, sheds and barns are ``Residential``, ``Commercial`` or ``Public`` and mostly have
  no address. Set it ``False`` for InfDB data with the class ``Auxiliary``: the outbuildings are out already, and the
  filter would leave out every building in the states whose LoD2 has no addresses (Bremen, Hessen, Saarland, Berlin,
  Brandenburg, Mecklenburg-Vorpommern and Sachsen in 2026). A run logs a warning when the value does not suit the
  InfDB data.
* ``RESIDENTIAL_ONLY_GENERATION: True`` keeps only residential buildings and components.
* Non-residential buildings that overlap a transformer candidate are removed; so are duplicates
  and buildings without geometry or ``objectid``. A removed building whose non-residential load is
  above ``MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW`` is still stored as MV customer.
* Buildings whose street-side connection point cannot be determined, and street components with
  at most one building, are removed later in the pipeline (:doc:`../concepts/pipeline`). A
  postcode with a single building keeps it and gets one grid.

Load components
~~~~~~~~~~~~~~~

Every building contributes up to two electrical load components at the same connection:

.. list-table::
   :header-rows: 1
   :widths: 22 42 36
   :class: fixed-table

   * - Component
     - Installed peak
     - Load units *N*
   * - Residential
     - ``households`` × ``peak_load`` of ``Residential`` (``PEAK_LOAD_HOUSEHOLD``)
     - number of households
   * - Commercial or Public
     - ``nonresidential_floor_area`` × ``peak_load_per_m2`` / 1000 (kW)
     - 1 per building

A non-residential component above ``MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW`` is assumed to be
supplied from the MV grid and is excluded; the residential component of the same building stays.
Such buildings are stored in ``pylovo.mv_customers``, where the MV stage takes them up as MV
customers (:ref:`mv-customers`).
How the components are combined into design loads is explained in
:doc:`../concepts/grid_dimensioning`.

The area split must be consistent: ``residential_floor_area`` and ``nonresidential_floor_area``
are both given or both missing, are non-negative, and add up to
``floor_area * floor_number`` (±0.01 m²). Otherwise the run stops with ``Invalid source building
area components``. ``building_use = 'Mixed'`` is not a load category of its own: it yields a
residential component and a Commercial or Public component.

Missing values
~~~~~~~~~~~~~~

pylovo keeps the InfDB values and fills only missing ones:

* ``floor_area`` -- the footprint area of ``geom``;
* residential or non-residential floor area -- ``floor_area × floor_number`` for the building
  types ``SFH``, ``TH``, ``MFH``, ``AB`` (residential) or ``Commercial``, ``Public``;
* ``households`` -- 1 for ``SFH`` and ``TH``; for ``MFH`` at least 2 and one per 181 m²
  residential floor area; for ``AB`` at least 5 and one per 146 m²; for untyped residential or
  mixed buildings at least 1 and one per 181 m². These assumptions are part of the version
  snapshot.

Street network
--------------

The street segments and connection lines of the postcode are copied into ``ways_tem_<plz>``.
``klasse`` is mapped to a numeric road class:

.. list-table::
   :header-rows: 1
   :widths: 60 40
   :class: fixed-table

   * - ``klasse``
     - class
   * - Bundesautobahn / Bundesstraße / Landesstraße, Staatsstraße / Kreisstraße
     - 11 / 13 / 15 / 21
   * - Gemeindestraße / Nicht öffentliche Straße
     - 41 / 51
   * - Wirtschaftsweg, Hauptwirtschaftsweg / Radweg / Fußweg
     - 71 / 81 / 91
   * - connection_line
     - 110
   * - any other value
     - 99

Segments of class 72 (``Rad- und Fußweg``) are not used for routing. The routing cost of every
segment is its length in metres. pylovo then connects the transformer candidates to the nearest
street segment and builds the pgRouting topology (:doc:`../concepts/pipeline`).

.. figure:: /images/generation/step2_connections.png
   :alt: Detail of the street graph with connection lines from building centroids to the street
   :width: 90%

   Connection lines link every building vertex to the street graph (detail of PLZ 85653). Demo
   extract derived from OpenStreetMap, © OpenStreetMap contributors, ODbL.

Postcode polygons
-----------------

``pylovo-setup`` copies all postcode polygons into ``pylovo.postcode``. A postcode that is still
missing when grids are generated is fetched from ``<INFDB_OPENDATA_SCHEMA>.postcodes_germany``
on demand. The polygon defines the area in which transformer candidates are selected.
