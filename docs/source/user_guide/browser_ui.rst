Browser UI (GridPlanner)
========================

GridPlanner is the browser UI for pylovo. It covers the whole workflow except classification:
set up the database, select regions, prepare transformer data and the configuration, generate
grids, and analyse them on the map with statistics, a grid inspector, an on-demand power flow,
diagnostics, 3D buildings and load editing. Long-running and database-writing steps run the
``pylovo-*`` commands of :doc:`cli` as background jobs whose logs are shown live, so the UI does
exactly what the command line does.

The UI lives in its own repository, **GridPlanner**, and talks to pylovo only through the
:doc:`http_api`; pylovo itself serves no web page. Steps 1–5 of GridPlanner's workflow are
pylovo. GridExpand can add steps 6–8 on the same grids (scenarios, time-series simulation and
grid expansion); without it, GridPlanner is the pylovo UI alone.

.. figure:: /images/ui/gridplanner-3d-feeders.png
   :alt: One grid on the map in 3D, cables and LoD2 buildings coloured by feeder, in the dark theme
   :width: 100%

   A large grid of the demo region (324 buildings, seven feeders, 2 × 630 kVA station): cables and
   3D buildings coloured by feeder, and cable cabinets with their number of outgoing cables. It was
   generated with larger stations than the default configuration allows.

.. list-table:: Where the parts live
   :header-rows: 1
   :widths: 30 70
   :class: fixed-table

   * - Part
     - Repository
   * - ``pylovo-api``
     - pylovo, ``api/`` (image ``ghcr.io/tum-ens/pylovo``, contract ``api/openapi.json``)
   * - The UI (``ui/``, plain ES modules, no build step)
     - GridPlanner
   * - The stack (``./gridplanner``, compose files, proxy)
     - GridPlanner
   * - The full UI guide, including the GridExpand steps
     - GridPlanner, ``docs/ui.md``

GridPlanner pins the pylovo image tag in its ``.env.example``. At start it checks the contract
version that ``pylovo-api`` reports (``/api/health``) and refuses versions it does not know.

Starting GridPlanner
--------------------

You need Linux with Docker Engine and the compose plugin (v2), and the credentials of the
InfDB/PostgreSQL database. A pylovo checkout is not needed: the pylovo image contains pylovo, GDAL
and ``pylovo-api``.

.. code-block:: bash

   git clone <GridPlanner repository> GridPlanner && cd GridPlanner
   ./gridplanner init --without-gridexpand   # asks for the database, writes .env
   ./gridplanner up                          # starts the stack, prints the URL

Then open http://127.0.0.1:18780/. Without ``--without-gridexpand``, ``init`` also asks for
GridExpand's data files and the UI shows steps 6–8. The database is the one in GridPlanner's
``.env`` (``DB_*``); it reaches ``pylovo-api`` as environment variables. pylovo's editable
``config/`` is ``state/pylovo/config/`` in GridPlanner. It is copied from the image at the first
start and edited in step 3.

``./gridplanner up --build`` builds the image from a pylovo checkout next to GridPlanner
(``../pylovo``, ``PYLOVO_SRC`` in ``.env``). Use it for a branch with API changes, or when no
published image fits. ``./gridplanner doctor`` checks the setup; GridPlanner's README describes
all commands.

The browser needs internet access: it loads the libraries (Vue, MapLibre GL, dockview, ECharts)
from jsDelivr and the basemaps from OpenFreeMap (no API key).

.. warning::

   The UI starts jobs that write to the database. It can reset the schema ``pylovo``, delete
   versions and overwrite the configuration. Setup only creates or migrates the schema. A reset,
   like every other destructive action, needs a typed confirmation (database name, version id,
   PLZ or DSO source) that the server checks again. Everything binds to ``127.0.0.1``, and there
   is no user management: put an authenticating proxy in front before exposing the stack.

Workspace
---------

The workspace consists of dockable panels: map, workflow, jobs, statistics, grid inspector, load
editor, configuration and transformers. You can drag panels to rearrange or split them, float
them, maximise them or pop them out into their own browser window. The browser keeps the layout;
*Reset layout* in the panels menu restores it. A panel that is closed and opened again returns to
its default place, and floating panels have a *Dock back* button.

The stepper in the top bar is the workflow (``Alt+1`` ... ``Alt+5`` for the pylovo steps). It
focuses the panels of each step and ticks a step only when it is done for the current selection.
The top bar also shows the database the API is connected to and the version the next run
generates.

The map shows the layers that matter in each step: postcode areas in Regions, transformer
candidates in Data, grids in Results. It remembers your own layer choices per step. Light and dark
themes follow the system setting and can be switched. ``?`` lists all keyboard shortcuts, for
example:

* ``/`` for the region search;
* ``J``/``K`` for the previous or next grid;
* ``R`` for the power flow;
* ``T``/``F``/``L``/``V`` for the map colouring.

Workflow
--------

.. list-table::
   :header-rows: 1
   :widths: 14 58 28
   :class: fixed-table

   * - Step
     - What you can do
     - Runs
   * - 1 Database
     - Connection, PostGIS/pgRouting versions, table counts, InfDB building count and pending
       schema migrations. Set up or migrate the schema (keeps all grids), or reset it after
       typing the database name.
     - ``pylovo-setup``, ``pylovo-setup reset --database NAME --yes``
   * - 2 Regions
     - Search postcodes (PLZ), municipality keys (AGS) or names, or click postcode areas on the
       map. Add all PLZ of a municipality at once. Clicking into the empty search field lists
       all postcodes with input data; only those can be selected (see *Region input check*).
     - --
   * - 3 Data
     - The input data of the PLZ. The transformer editor: place by click, set ratings, delete,
       uniform or percentage bulk ratings, clear. A table of all candidates by source (OSM, DSO,
       manual, LoD2). OSM import by relation id with a name lookup, DSO CSV upload with a preview,
       and the generation configuration.
     - ``pylovo-import transformers-osm``, ``pylovo-import transformers-dso-csv``
   * - 4 Generate
     - A pre-check per PLZ (new, exists, blocked, not verified) and of the saved configuration
       against the stored parameters of ``VERSION_ID``. Switch to a new version with a comment,
       or regenerate existing PLZ after a typed confirmation. Parallel processing, ``--plz`` or
       ``--ags``. A live log, an outcome per PLZ with *Retry*, and the analysis.
     - ``pylovo-generate``, ``pylovo-delete networks``, ``pylovo-analyze --all``
   * - 5 Results
     - Versions (compare, delete) and the PLZ of a version (export, delete). Statistics, the
       grid map, the grid inspector with diagnostics, an on-demand power flow and load editing.
     - ``pylovo-delete``, ``pylovo-export``

Only one database-writing job runs at a time, and direct edits of transformers or loads are
refused while one runs. Cancelling a job that writes to the database asks for a confirmation that
says what the cancellation leaves behind. Job logs survive a restart; they are kept in
``state/pylovo/api/`` of GridPlanner.

.. figure:: /images/ui/gridplanner-generate.png
   :alt: Generate step with the version of the next run, the per-PLZ pre-check and the job log
   :width: 100%

   Generate step: the version of the next run checked against its stored parameters, the state of
   each selected PLZ, the last run and the jobs with their live log.

Region input check
------------------

Generation needs building and street input for a postcode area. The UI lets you select a PLZ for
generation only when the :ref:`region input check <http-api-region-check>` of the API finds that
input. Other postcodes are shown muted in the search results and dashed on the map. Clicking one
opens a card with the reason and what to do about it. Postcodes whose check has not finished yet
stay selectable and are marked *not verified*. Existing results always stay viewable, analysable
and exportable.

To find the postcodes that can be generated, click into the empty search field. It lists every
checked postcode with input data, grouped by municipality, with its number of importable
buildings and its generated versions. For a typed search, the switch *only with input data* hides
the other postcodes.

.. figure:: /images/ui/gridplanner-regions.png
   :alt: Regions step with a postcode that has input data and postcodes without a polygon
   :width: 100%

   Regions step: the postcode with input data shows its importable buildings and its version; the
   others show why they cannot be generated. The card below lists the input data of the PLZ.

.. figure:: /images/ui/gridplanner-data.png
   :alt: Data step with the transformer candidates of a postcode area on the map and in a table
   :width: 100%

   Data step: transformer candidates of the postcode area by source on the map and in the
   *Transformers* table, next to the editor tools (:doc:`transformer_data`).

Configuration editor
--------------------

The *Config* panel edits ``config/config_generation.yaml``. It has a form for the key generation
parameters, with the YAML comments as help texts, and a YAML editor for everything else. Form
edits rewrite only the changed keys, so comments and layout stay. *Review & save* shows the diff
and validates the file with pylovo's own loader, which catches every error pylovo itself would
raise. The editor refuses to overwrite a file that changed on disk, and *Backups* restores an
earlier version. Remember to change ``VERSION_ID`` when you change generation parameters
(:doc:`generating_grids`); step 4 warns when the saved configuration no longer matches the
stored parameters of the version.

.. figure:: /images/ui/gridplanner-config.png
   :alt: Configuration form with version, consumer and cable dimensioning parameters
   :width: 100%

   Configuration form: each field shows its YAML key and the comment from the file.

Statistics and grid inspector
-----------------------------

The statistics panel shows key figures and charts for a version and postcode area:

* stations and their sizes (a station of two parallel 400 kVA units counts once, as
  ``2 × 400 kVA``) and transformer loading;
* the voltage budget of the weakest consumer against the voltage band, and design compliance
  (feeder and service voltage-drop limits);
* cable length by type, households, feeders and distances;
* a sortable grid table with a column picker;
* a comparison of two versions, including the generation parameters that differ.

Every table can be downloaded as CSV. In Results the map shows only the stations the grids use;
transformer candidates that no grid uses appear in the Data step only. The PLZ overview can colour
grids by grid, by generation-check status, by utilisation or by design voltage drop.

.. figure:: /images/ui/gridplanner-results.png
   :alt: Results step with every grid of a postcode area on the map and the statistics panel
   :width: 100%

   Results of a version: every grid of the postcode area on the map (one colour per grid,
   stations with their rating) next to the statistics panel.

Clicking a grid, or ``J``/``K``, opens the grid inspector:

* the limits the grid misses and its key figures;
* the *generation check*: the validation power flow that pylovo stored with the grid, read for
  four criteria (see :ref:`generation check <http-api-generation-check>`);
* the *on-demand power flow*, which reruns that operating point with a load scaling factor and
  writes nothing. At ×1 it reproduces the generation check;
* the feeder, cable, bus, cabinet and consumer tables, whose rows highlight on the map;
* the pandapower JSON download and a CSV export.

The map colours the cables of the grid by cable type (width by parallel count) or by feeder. After
a power flow it colours cables by loading and buses by voltage, and the inspector shows a voltage
profile over the cable distance from the station.

.. figure:: /images/ui/gridplanner-inspector-dark.png
   :alt: Grid inspector in the dark theme with cables coloured by loading and a voltage profile
   :width: 100%

   Grid inspector (dark theme) after the on-demand power flow: cables coloured by loading, the
   generation check with its voltage budget, and the voltage profile of every bus over its cable
   distance from the station.

**Cable cabinets.** pylovo's feeder split points are drawn as cable cabinets: box symbols with the
number of outgoing cables. They are distinct from the connection points on the feeders and never
at the station busbar. They are named K1 … Kn by cable distance from the station, the same names
the diagnostics use, and are listed in the inspector's *Cabinets* tab. Service connections
directly at the station busbar are shown as direct connections, not as feeders.

**3D buildings.** With *3D buildings (LoD2)* in the map's layer menu (on by default), the
buildings of the selected grid are drawn as their LoD2 models (:ref:`LoD2 meshes <http-api-lod2>`; figure at
the top of this page). Roofs take the building's map colour: neutral, or its feeder's colour when the
map is coloured by feeder. Walls take a light tint of it. The map tilts the first time the models
appear and stays flat if you flatten it. All other buildings stay 2D, and so do grid buildings
without an LoD2 model; a database without ``citydb`` is not an error. The layer menu shows how
many of the grid's buildings have a model. The models stand on the map plane (there is no
terrain) and are drawn below the cables, so the grid stays readable. The 2D footprints underneath
stay clickable.

Diagnostics
-----------

The *Diagnostics* tab of the inspector explains why a grid misses a limit. It lists the findings of
the :ref:`diagnostic rules <http-api-diagnostics>`:

* symptoms, such as the voltage band at the weakest consumer, cable overload or transformer
  loading;
* their causes, each with the share of the symptom it explains;
* practice and data findings.

Findings are grouped by symptom and can be filtered by severity, category or feeder. Before a power
flow the findings are estimates from the design data and the generation check. *Exact values*
runs the power flow, and a load scaling other than ×1 shows which findings are new. A voltage
budget per feeder shows how the drop builds up from the transformer through the largest sections
to the consumer. Clicking a finding highlights its cables, buses, buildings or cabinets on the map.
A data finding about a building links to the load editor.

.. figure:: /images/ui/gridplanner-diagnostics.png
   :alt: Diagnostics tab with the voltage budget of a feeder, the findings and the path on the map
   :width: 100%

   Diagnostics of a grid with a voltage-band violation: the voltage budget of feeder 1, the
   findings by severity and category, and the path to the weakest consumer highlighted on the map.

.. _browser-ui-load-editing:

Editing loads
-------------

The *Load editor* corrects the input of one building. Open it in one of these ways:

* click a building on the map;
* click a row in the inspector's *Consumers* or *Buses* tab;
* click *Edit building* on a diagnostics finding.

You can change the households and the non-residential floor area and use. Before anything is
written, the editor previews the effect: the building's peak load, the grid's coincident load and
the validation power flow before and after. *Apply to database* stores the edit in one transaction
(:ref:`http-api-load-editing`); the first edit of a version needs its id typed in. Cables and the
transformer stay as generated: to redimension the grid, correct the input and regenerate the PLZ
as a new version. Every edit can be undone exactly (the latest first), or all edits of a grid at
once. Edited grids and versions are marked in the map, the inspector, the statistics and the
version cards.

.. figure:: /images/ui/gridplanner-load-editor.png
   :alt: Load editor below the grid inspector with a changed household count and its preview
   :width: 100%

   Load editor: the building flagged by the diagnostics, with a changed household count and the
   preview of its peak load before anything is written.

More
----

GridPlanner's ``docs/ui.md`` is the full user guide of the UI, including the GridExpand steps and
the code layout. Its ``docs/ARCHITECTURE.md`` describes the contracts and versions, and its
browser tests are in ``tests/ui``. On the pylovo side, ``api/README.md`` lists the endpoints.

The screenshots show the demo region 85653 (Aying), built from an OpenStreetMap extract with
synthetic LoD2 models; map data © OpenStreetMap contributors (ODbL), basemap OpenFreeMap /
OpenMapTiles.
