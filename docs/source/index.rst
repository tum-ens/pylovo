pylovo
======

Detailed low-voltage grid data are often unavailable for regional energy studies. **pylovo**
(PYthon tool for LOw-VOltage distribution grid generation) turns open building, street and
transformer data into geographically located synthetic grids for German postcode areas. These
models make it possible to explore local network constraints, future demand and grid planning
questions across many places. Results can be explored in the browser with GridPlanner
(:doc:`user_guide/browser_ui`) and used as `pandapower <https://www.pandapower.org/>`_ networks or
GIS data.

.. figure:: /images/ui/gridplanner-3d-feeders.png
   :alt: One generated grid in the GridPlanner UI in 3D, cables and buildings coloured by feeder
   :width: 100%

   Explore generated grids in the :doc:`user_guide/browser_ui`: one grid of the demo region 85653
   (Aying) with its feeders and 3D buildings. Demo extract derived from OpenStreetMap with
   synthetic LoD2 models; map data © OpenStreetMap contributors (ODbL), basemap OpenFreeMap /
   OpenMapTiles.

.. rubric:: Key features

* **Regional open-data inputs** -- `InfDB <https://github.com/tum-ens/InfDB>`_ harmonises LoD2
  buildings, Zensus 2022 information, official street data and postcode areas; transformer
  positions can also come from OpenStreetMap or network operators (:doc:`user_guide/input_data`).
* **Brownfield and greenfield generation** -- incorporate known transformer locations where
  available and place synthetic transformers for the remaining consumers.
* **Geographic and electrical detail** -- connect buildings along streets, route radial feeders,
  size equipment for coincident loads and voltage limits, and check the grids with a power flow
  (:doc:`concepts/grid_dimensioning`).
* **Reproducible versions** -- every run belongs to a ``VERSION_ID`` whose generation parameters
  are frozen in the database; generation is deterministic for a given configuration.
* **Scales from one postcode to many** -- ``pylovo-generate`` accepts postcodes (PLZ) or
  municipalities (AGS) and runs postcodes in parallel.
* **Analysis and visualisation** -- key figures per postcode and per grid, QGIS templates,
  CSV export, plotting helpers, and the GridPlanner browser UI on top of pylovo's HTTP API
  (:doc:`user_guide/browser_ui`).

.. rubric:: Where to start

.. grid:: 1 2 2 3
   :gutter: 3

   .. grid-item-card:: Installation
      :link: getting_started/installation
      :link-type: doc

      Python environment, PostgreSQL with PostGIS and pgRouting, InfDB.

   .. grid-item-card:: Quickstart
      :link: getting_started/quickstart
      :link-type: doc

      ``.env``, ``pylovo-setup`` and your first ``pylovo-generate`` run.

   .. grid-item-card:: Configuration
      :link: user_guide/configuration
      :link-type: doc

      Every key of ``config_generation.yaml``, ``config_analysis.yaml`` and ``.env``.

   .. grid-item-card:: Command-line reference
      :link: user_guide/cli
      :link-type: doc

      ``pylovo-setup``, ``-generate``, ``-analyze``, ``-import``, ``-export``, ``-delete``.

   .. grid-item-card:: How grids are generated
      :link: concepts/pipeline
      :link-type: doc

      The pipeline step by step on the demo region PLZ 85653.

   .. grid-item-card:: API reference
      :link: api_reference
      :link-type: doc

      Modules, classes and functions of the ``pylovo`` package.

.. rubric:: Citation

If you use pylovo in a scientific publication, please cite:

   Reveron Baecker et al. (2025): *Generation of low-voltage synthetic grid data for energy
   system modeling with the pylovo tool*. https://doi.org/10.1016/j.segan.2024.101617

Further publications are listed in :doc:`further_reading`.

.. rubric:: Licence

pylovo is open source under the `MIT License <https://opensource.org/license/MIT>`_,
copyright © TUM ENS (see ``LICENSE.txt``). The source code is on
`GitHub <https://github.com/tum-ens/pylovo>`_ and open for collaboration.

.. rubric:: Acknowledgement

The development of this software has been supported by contributions of the following persons:
Soner Candas, Deniz Tepe, Tong Ye, Daniel Baur, Julian Zimmer and Berkay Olgun.

.. toctree::
   :hidden:
   :caption: Getting started

   getting_started/installation
   getting_started/quickstart
   getting_started/tutorials

.. toctree::
   :hidden:
   :caption: User guide

   user_guide/configuration
   user_guide/database_setup
   user_guide/input_data
   user_guide/transformer_data
   user_guide/region_selection
   user_guide/generating_grids
   user_guide/analysing_grids
   user_guide/applications
   user_guide/exporting_visualising
   user_guide/browser_ui
   user_guide/http_api
   user_guide/cli
   user_guide/troubleshooting

.. toctree::
   :hidden:
   :caption: Concepts

   concepts/architecture
   concepts/pipeline
   concepts/grid_dimensioning
   concepts/database_schema
   concepts/database_tables
   concepts/electrical_backends

.. toctree::
   :hidden:
   :caption: Reference

   api_reference

.. toctree::
   :hidden:
   :caption: Project

   development/index
   further_reading
