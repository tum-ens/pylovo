Input data
==========

A grid is built from three inputs per postcode area: **buildings** (geometry, use, households,
floor areas), the **street network** and the **postcode polygon**. Transformer positions are an
optional fourth input (:doc:`transformer_data`). pylovo reads the first three either from InfDB
(``USE_INFDB=True``, default) or from files (``USE_INFDB=False``).

.. figure:: /images/generation/step1_inputs.png
   :alt: Buildings coloured by use, street network and transformer candidates of PLZ 85653
   :width: 90%

   Input data of the demo region PLZ 85653 (Aying): buildings by use, streets and transformer
   candidates. Demo extract derived from OpenStreetMap, © OpenStreetMap contributors, ODbL;
   figure made with ``docs/scripts/plot_generation_steps.py``.

InfDB input (``USE_INFDB=True``)
--------------------------------

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

With ``USE_INFDB=True`` pylovo reads these tables from the database configured in ``.env``:

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
``Residential``, ``Commercial``, ``Public`` or ``Mixed``. Further rules:

* Buildings with function code ``31001_2523`` (transformer stations in LoD2) are not consumers;
  they are added to the transformer candidates (:doc:`transformer_data`).
* ``EXCLUDE_BUILDINGS_WITHOUT_ADDRESS: True`` leaves out buildings without street or house number.
* ``RESIDENTIAL_ONLY_GENERATION: True`` keeps only residential buildings and components.
* Non-residential buildings that overlap a transformer candidate are removed; so are duplicates
  and buildings without geometry or ``objectid``.
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
on demand. The polygon defines the area in which transformer candidates are selected; in the
file-based mode it also selects buildings and streets.

.. _file-based-input:

File-based input (``USE_INFDB=False``)
--------------------------------------

This mode does not read InfDB schemas. The inputs are files in the data directory (``./data``, or
``PYLOVO_DATA_DIR``, see :ref:`env-variables`). The repository holds the folders ``buildings/`` and
``ways/`` with a note each; ``postcode.csv`` and your own files are not in git. The reference data
(municipal register, transformers) ships with pylovo.

.. list-table::
   :header-rows: 1
   :widths: 38 62
   :class: fixed-table

   * - File
     - Content
   * - ``postcode.csv``
     - Postcode polygons for ``pylovo.postcode``, loaded by ``pylovo-setup`` (see below).
   * - ``ways/ways_public_2po_4pgr.sql``
     - Street network as SQL dump created with osm2po, loaded by ``pylovo-setup`` into
       ``pylovo.ways`` (see below).
   * - ``buildings/Res_<AGS>.shp``, ``buildings/Oth_<AGS>.shp``
     - Building shapefiles per municipality, imported when a postcode is generated (see below).

``pylovo-setup`` stops before it changes anything if ``postcode.csv`` or the street dump is missing.

Buildings are imported when a postcode is generated: pylovo looks up the AGS of the postcode in
``pylovo.municipal_register``, imports the matching shapefiles with ``ogr2ogr`` into the tables
``res`` and ``oth`` and records the AGS in ``ags_log`` so that it is imported only once. A
postcode can span several municipalities; all of them are imported. When you generate one postcode
(``pylovo-generate --plz <plz>``), every AGS of it that is not in ``ags_log`` yet needs a shapefile: if one has
none, pylovo stops with a ``FileNotFoundError`` that names the AGS and the folder, before it imports
anything, so you can add the files and run again. With several postcodes or ``--ags``, AGS without a
file are skipped and tried again at the next run; the grids then contain only the buildings that were
imported. Buildings are assigned to the postcode by their centroid, streets by intersection with the
postcode polygon. In this mode pylovo creates the building connection lines itself (functions
``segment_intersecting_ways`` and ``generate_building_to_way_connections``).

Postcode polygons (``postcode.csv``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The file is a comma-separated text file with a header line, read with ``pandas.read_csv``. ``pylovo-setup``
renames the column ``einwohner`` to ``population`` and ``gid`` to ``postcode_id`` and appends all rows to
``pylovo.postcode`` as they are. The header must therefore name columns of that table: columns that the file
does not have stay empty (the database numbers ``postcode_id`` itself), a column that is not in the table
makes the import fail.

.. list-table::
   :header-rows: 1
   :widths: 24 12 64
   :class: fixed-table

   * - Column
     - Required
     - Content
   * - ``plz``
     - yes
     - Postcode as a whole number, unique.
   * - ``geom``
     - yes
     - Polygon of the postcode area as text that PostGIS reads as a geometry: hexadecimal EWKB (what
       ``psql \copy`` writes for a PostGIS column) or EWKT such as
       ``SRID=25832;MULTIPOLYGON(((x y, x y, ...)))``. The table column is
       ``geometry(MultiPolygon, <TARGET_EPSG>)``: the file is not reprojected, so the coordinates and the SRID
       must be those of ``TARGET_EPSG`` (metres), and a ``POLYGON`` has to be written as a ``MULTIPOLYGON``.
       The polygon selects the buildings and streets of the postcode.
   * - ``note``
     - no
     - Name of the postcode area (text).
   * - ``qkm``
     - no
     - Area in km² (number).
   * - ``einwohner``
     - no
     - Inhabitants (whole number), stored as ``population``.
   * - ``gid``
     - no
     - Row number (whole number), stored as ``postcode_id``.

A file of this form can be exported from a PostGIS table of postcode polygons, for example with
``\copy (SELECT plz, note, qkm, einwohner, ST_Multi(ST_Transform(geom, 25832)) AS geom FROM <table>) TO
'postcode.csv' CSV HEADER`` in ``psql``.

Building shapefiles (``buildings/``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Put one pair of shapefiles per municipality into ``buildings/``, for example ``Res_9162000.shp`` and
``Oth_9162000.shp`` with their ``.shx``, ``.dbf`` and ``.prj`` files. A file belongs to an AGS if the AGS as it is
stored in ``pylovo.municipal_register.ags`` appears in its name (the municipality key without a leading zero, for
example 9162000 for Munich; ``09162000`` in the name matches as well). It goes into the table ``oth`` if its name
contains ``Oth`` and into ``res`` if it contains ``Res``; the folder names do not count, and a name with neither
is refused.

The polygons need a CRS (the ``.prj`` file): ``ogr2ogr`` reprojects them to ``TARGET_EPSG`` and promotes them to
multipolygons. The attributes are matched by name to the columns of ``res`` and ``oth``
(:doc:`../concepts/database_tables`); a DBF field name has at most 10 characters, hence ``building_t``. The
generation reads only these attributes, the other columns are not used:

.. list-table::
   :header-rows: 1
   :widths: 24 16 60
   :class: fixed-table

   * - File
     - Attribute
     - Content
   * - ``Res_<AGS>.shp``
     - ``osm_id``
     - Id of the building (text), unique over all files because it is the primary key of ``res``.
   * - ``Res_<AGS>.shp``
     - ``building_t``
     - Building type ``SFH`` (single-family house), ``TH`` (terraced house), ``MFH`` (multi-family house)
       or ``AB`` (apartment block). It decides the households: one for ``SFH`` and ``TH``, at least two for
       ``MFH`` and at least five for ``AB``, from the floor area.
   * - ``Res_<AGS>.shp``
     - ``area``
     - Footprint in m²; the polygon area if empty.
   * - ``Res_<AGS>.shp``
     - ``floors``
     - Number of floors (whole number); one if empty.
   * - ``Oth_<AGS>.shp``
     - ``osm_id``
     - Id of the building, unique as above (primary key of ``oth``).
   * - ``Oth_<AGS>.shp``
     - ``use``
     - ``Commercial`` or ``Public``; other rows are ignored.
   * - ``Oth_<AGS>.shp``
     - ``area``
     - Footprint in m²; the polygon area if empty. These buildings have one floor.

Street network with osm2po
~~~~~~~~~~~~~~~~~~~~~~~~~~

The SQL dump is created from an OpenStreetMap extract with osm2po 5.3.6 (later versions are not
supported by these instructions):

#. Download the OSM extract (``.pbf``) from `Geofabrik <https://download.geofabrik.de/>`_ and
   osm2po 5.3.6 from `osm2po.de <https://osm2po.de/releases/>`_.
#. In ``osm2po.config`` set ``tilesize=x``, comment out ``.default.wtr.finalMask = car``, keep
   only ``ferry`` commented out in the way-type section, and keep the SQL writer line active.
#. Run ``java -Xmx1g -jar osm2po-core-5.3.6-signed.jar prefix=public path/to/extract.pbf``.
#. Copy the resulting ``public_2po_4pgr.sql`` to ``ways/ways_public_2po_4pgr.sql`` in the data
   directory and run ``pylovo-setup``.

The dump creates the table ``public_2po_4pgr`` (osm2po's standard output with the columns ``id``, ``source``,
``target``, ``clazz``, ``cost``, ``reverse_cost`` and ``geom_way``). ``pylovo-setup`` copies it into
``pylovo.ways`` with the geometry transformed to ``TARGET_EPSG`` and drops it.
