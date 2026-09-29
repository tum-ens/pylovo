HTTP API
========

``pylovo-api`` is a headless HTTP API (FastAPI) for the whole pylovo workflow except
classification: set up the database, select regions, prepare transformer data and the
configuration, generate grids, and read statistics, grid details, diagnostics and power flows.
Long-running and database-writing steps run the existing ``pylovo-*`` commands of :doc:`cli` as
background jobs whose logs can be followed live, so the API does exactly what the command line
does.

The browser UI that uses this API lives in the GridPlanner repository (:doc:`browser_ui`).
GridPlanner serves the UI and puts the API behind a reverse proxy (``/pylovo/api/*`` to ``/api/*``
of ``pylovo-api``); pylovo itself serves no web page.

Starting the API
----------------

.. code-block:: bash

   uv sync --extra api
   uv run --extra api pylovo-api      # http://127.0.0.1:8765/api/health, API docs at /docs

``pylovo-api`` looks for ``config/config_generation.yaml`` in ``--root DIR``, ``$PYLOVO_ROOT``,
the current directory and its parents, and the checkout the package was installed from. The
database is the one in the project's ``.env`` (or in the environment variables ``DBNAME``,
``DBUSER``, ``PASSWORD``, ``HOST`` and ``PORT``), exactly as for the command line. Other options:
``--host`` (default ``127.0.0.1``), ``--port`` (default ``8765``), ``--log-level`` and
``--allowed-host`` (also ``PYLOVO_API_ALLOWED_HOSTS``) for the host name of a reverse proxy.

The container image ``pylovo`` (``api/docker/Dockerfile``, published as
``ghcr.io/tum-ens/pylovo``) contains pylovo, GDAL and the API and starts
``pylovo-api --host 127.0.0.1 --port 18765``.

.. warning::

   The API can reset the schema ``pylovo`` (``POST /api/jobs/reset``), delete versions and overwrite
   the configuration. ``POST /api/jobs/setup`` only runs the non-destructive ``pylovo-setup``: it
   creates a missing schema or applies pending migrations (``pending_migrations`` in
   ``/api/status``) and keeps all grids. API contract version 2 introduced this split; in version 1
   the setup job reset the schema. It binds to ``127.0.0.1`` by default, rejects requests with a foreign ``Host``
   header and requires the header ``X-Pylovo-UI: 1`` for every state-changing call. Destructive
   actions need a typed confirmation (database name, version id, PLZ or DSO source) that the
   server checks. There is no user management: only bind to another interface on a trusted
   network.

.. note::

   The server runs its code from memory. After a ``git checkout`` or ``git pull``,
   ``/api/status`` reports the code as stale (``ui.code.stale``) until you restart the server;
   requests to new features fail until then.

Contract
--------

The GridPlanner UI relies on the routes, parameters and response shapes of the API; they stay
stable. ``GET /api/health`` answers without database access with the service name, the contract
version ``api``, the pylovo package version and the git revision of the running code. The
contract version is increased only for breaking changes, and the UI refuses versions it does not
know. The committed OpenAPI snapshot ``api/openapi.json`` is checked by a unit test and, on pull
requests, by ``oasdiff breaking`` against the base branch (see ``api/README.md``).

Jobs
----

Setup, generation, analysis, deletion, export and the transformer imports are jobs: the API runs
the corresponding command with the project root as working directory and streams its output as
Server-Sent Events. Only one database-writing job runs at a time, and direct edits of transformers
or loads are refused while one runs. Job logs survive a restart of the API
(``.pylovo-api/jobs/`` in the project).

.. _http-api-region-check:

Region input check
------------------

Generation needs building and street input for a postcode area. The API therefore reports for
each PLZ whether it can be generated: it needs a postcode polygon, at least two buildings that
pylovo would import (the same filters as ``pylovo-generate``, including
``EXCLUDE_BUILDINGS_WITHOUT_ADDRESS`` and ``RESIDENTIAL_ONLY_GENERATION``), at least one
residential building, street segments and connection lines. Postcodes whose check has not
finished yet are reported as *not verified*. Existing results always stay readable, analysable
and exportable, and the server refuses to generate a blocked PLZ unless it is included
explicitly.

On a large InfDB the check runs in the background per municipality key, using the indexes the
InfDB schema creates; postcodes that are asked for are checked first. The results are cached in
``.pylovo-api/cache/input-coverage.json`` and refreshed when the input tables change. An index on
``postcode`` of ``basedata.buildings``, ``ways_per_connection`` and ``connection_lines`` makes the
check (and every ``pylovo-generate`` run) faster. ``PYLOVO_API_COVERAGE_ENFORCE=0`` turns blocks
into warnings.

Configuration editing
---------------------

The API edits ``config/config_generation.yaml`` either as text or through a form of the key
generation parameters (the help texts are the YAML comments). Form edits rewrite only the changed
keys, so comments and layout stay. Every save can be previewed as a diff, validates the YAML and
imports ``pylovo.config_loader`` against a temporary copy (this catches every error pylovo itself
would raise), refuses to overwrite a file that changed on disk, and keeps a backup that can be
restored. Remember to change ``VERSION_ID`` when you change generation parameters
(:doc:`generating_grids`).

Statistics and grid details
---------------------------

For a version and postcode area the API returns key figures: stations and their sizes (a station
of two parallel 400 kVA units counts once, as ``2 × 400 kVA``), transformer loading, the voltage
budget of the weakest consumer against the voltage band, design compliance (feeder and service
voltage-drop limits), cable length by type, households, feeders and distances, and a comparison
of two versions including the generation parameters that differ. The grids of a PLZ and the
detail of one grid (cables, buses, feeders, consumers, cabinets) come as GeoJSON; the pandapower
network can be downloaded.

.. _http-api-generation-check:

**Generation check and on-demand power flow.** pylovo runs a validation power flow when it saves
each grid, at the transformer-coincident operating point, and stores the solved network with the
grid. The API reads this *generation check* for four criteria: solver converged, voltage band
(``POWER_FLOW_VOLTAGE_LIMITS``), cables at most 100 % loaded, and transformer loading against the
planning utilisation. The *on-demand power flow* reruns the same stored operating point,
optionally scaled, and writes nothing; at ×1 it reproduces the generation check.

**Cable cabinets.** pylovo's feeder split points are reported as cable cabinets with their number
of outgoing cables, never at the station busbar. They are named K1 … Kn by cable distance from the
station, the same names the diagnostics use. Service connections directly at the station busbar
are direct connections, not feeders.

.. _http-api-lod2:

**3D buildings.** The LoD2 models of a grid's buildings are read from the InfDB schema ``citydb``
(3DCityDB v5), linked by the building ``objectid``, and sent as a small binary mesh. Buildings
without an LoD2 model, or a database without ``citydb``, give an empty mesh and never an error.

.. _http-api-diagnostics:

Diagnostics
-----------

The diagnostics explain why a grid misses a limit. Their 26 rules follow DSO planning practice
and fall into three kinds:

* **symptoms** say what is wrong: voltage band at the weakest consumer, cable overload,
  transformer loading, pylovo's feeder or service design limit, the generation check;
* **causes** say why, each with the share of the symptom it explains, e.g. a long feeder, a
  section dimensioned too small compared with its neighbours, a large load at the feeder end, dense
  demand, too many households for one cable, unbalanced feeders or an eccentric station;
* **practice and data findings** flag deviations from planning practice or implausible input,
  e.g. a building whose households do not fit its floor area.

Without a power flow the rules use the design data and the generation check (estimates are
marked); with the on-demand power flow they use its exact results, and a load scaling other than
×1 shows which findings are new. A voltage budget per feeder shows how the drop builds up from 1.0
p.u. through the station (down to the LV busbar, which the validation power flow puts at
``LV_REFERENCE_VOLTAGE_PU``) and the largest sections to the consumer. Thresholds that pylovo
defines come from the version's stored parameters. The heuristic ones can be overridden in an optional
``GRID_DIAGNOSTICS`` block of ``config_analysis.yaml``; the keys are those of ``DEFAULT_THRESHOLDS``
in ``api/pylovo_api/diagnostics.py``, and tables such as ``grouping_factors`` can be changed
entry by entry:

.. code-block:: yaml

   GRID_DIAGNOSTICS:
     long_feeder_m: 400        # a feeder longer than this is reported as long
     overload_warning: 0.85    # warn from 85 % of the cable ampacity

Diagnostics never write to the database.

.. _http-api-load-editing:

Editing loads
-------------

A building's input can be corrected: the households and the non-residential floor area and use.
A preview shows the effect before anything is written: the building's peak load, the grid's
coincident load (simultaneity is grouped per category over the whole grid, so other loads change
slightly too) and the validation power flow before and after.

Applying an edit writes in one transaction: the building row, the grid's pandapower loads and
stored network, the generation-check results and an audit row in ``pylovo.load_edit``. The
parameters are always those of the version's stored snapshot. Cables and the transformer stay as
generated: to redimension the grid, correct the input and regenerate the PLZ as a new version. The
first edit of a version needs its id as confirmation. Analysis rows of the PLZ are removed because
they no longer match; a re-analysis rebuilds them. Every edit can be undone exactly (the latest
first), or all edits of a grid at once, and edited grids and versions are flagged in the results.
The edit history of a version can be downloaded as CSV. Before editing, the API checks that the
stored loads are reproduced exactly from the building inputs; if they are not, the grid cannot be
edited and has to be regenerated.

More
----

``api/README.md`` lists the endpoints by router and describes the architecture, the contract
rules and the tests. The tests in ``api/tests`` use the FastAPI test client; database tests only
run against a database you name explicitly (``PYLOVO_API_TEST_DATABASE``).
