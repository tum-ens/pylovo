.. image:: docs/source/images/logo.png
    :width: 700
    :alt: Default view

**A repo to generate synthetic low-voltage distribution grids based on open data.**

.. list-table::
   :widths: auto

   * - License
     - |badge_license|


pylovo (PYthon tool for LOw-VOltage distribution grid generation)
========================================================================

Overview
------------------------------------------------------------------------

pylovo is a Python-based tool for generating synthetic low-voltage (LV) distribution grids using open data sources.
Designed for energy system modeling research, it generates realistic and analyzable grid models for user-defined geographic areas.
This enables researchers and practitioners to explore critical questions of the energy transition—such as the analysis
of the potential integration of distributed energy resources and how their flexibilities can be leveraged while accounting
for electricity grid constraints.


Key Features
------------------------------------------------------------------------

* **Synthetic Grid Generation**: Creates realistic LV distribution network topologies (transformers, feeder cables, house connections, ...) based on available geodata
* **Comprehensive Data Preprocessing**: Uses a reproducible and scalable preprocessing pipeline using harmonized open datasets (see section "Data Sources & Preprocessing Pipeline")
* **Geospatial Processing**: Utilizes PostgreSQL databases with PostGIS for efficient geographic data handling
* **Power System Integration**: Provides grid models in the format of existing simulation frameworks (Default: pandapower, WiP: OpenDSS)
* **Automated Analysis**: Provides comprehensive grid statistics and performance metrics for evaluating the generated networks
* **Visualization Support**: Includes tools for grid visualization and analysis using QGIS and geopandas

Regional Coverage
------------------------------------------------------------------------
* The default data currently supports all regions of Bavaria (extending to full Germany within the running `NEED <https://need.energy/>`_ project)
* Other countries can be added by two steps:

  1. Add an input data preprocessing pipeline for the respective country (e.g., see a parallel project working on the `US-pipeline <https://github.com/DAI-Lab/Gridtracer>`_).
  2. Adjust parameters in the ``config_generation.yaml`` file to consider specific equipment or grid dimensioning strategies across different countries.

Data Sources & Preprocessing Pipeline
------------------------------------------------------------------------
pylovo leverages a reproducible and scalable open-data-based preprocessing pipeline within a dockerized environment that integrates multiple harmonized datasets into a unified Infrastructure Database (`InfDB <https://github.com/tum-ens/InfDB>`_):

Main data sources:

* **3D Building Models (LoD2)**: High-resolution 3D building geometries as a base of various other data modeling tools
* **Census Demographics (Zensus 2022)**: Official statistical data on the population, households and buildings
* **Cadastral Geodata**: Official street networks with addresses and administrative boundaries
* **OpenStreetMap (OSM)**: Transformer locations and additional street network data

Main processing steps for pylovo:

* **Building Type Classification**: Building type allocation based on geometry and demographic data
* **Household Allocation**: Statistical assignment of households to buildings
* **Street Graph Construction**: Network routing consistent with cadastral information for realistic grid topology

User Data Requirements
------------------------------------------------------------------------

Pylovo requires user-provided geospatial data in the ``raw_data/`` directory:

**Note**: When using InfDB, building and street data are fetched directly from the database, reducing the need for local shapefiles.

* **Building shapefiles**: Place building geometries (as ``.shp`` files) in ``raw_data/buildings/``. Files should be named with AGS codes (e.g., ``09162000.shp`` for a specific municipality). These are typically obtained from official cadastral sources or InfDB preprocessing.

* **Street network SQL**: Place the OSM-derived street network SQL file (``ways_public_2po_4pgr.sql``) in ``raw_data/ways/``. This file is generated using the `osm2po <http://osm2po.de/>`_ tool from OpenStreetMap data.

* **Transformer data** (optional): Import from OpenStreetMap with ``pylovo-import transformers-osm --relation-id <99999>`` (or use prebuilt raw_data files for bavaria).

Quick Start
------------------------------------------------------------------------
0. **Requirements**: Python 3.12+, Docker, Ubuntu WSL2 or Linux-based OS, uv

1. | **Setup InfDB**:
   | Follow the documentation in `InfDB <https://tum-ens.github.io/InfDB/usage/>`_ from release 2.0.0 to set up the infdb database for pylovo.
   | Summarized you have to built three docker container:

   a) ...first, initialize the database with infdb-db service ``bash infdb-start.sh up -d --build``
   b) ...second, configure and import the required data with infdb-import service ``bash infdb-import.sh``
   c) ...third, run the required preprocessing basedata tool steps ``bash tools/tools.sh -p basedata-buildings <ags>`` and ``bash tools/tools.sh -p basedata-ways <ags>``

2. | **Install pylovo**:
   | Input data has been prepared, next lets focus on pylovo - setup the repository as follows:

   a) If not already installed, download uv to install and manage your Python environment.

   .. code-block:: bash

      curl -LsSf https://astral.sh/uv/install.sh | sh

   b) Clone the pylovo repository and install dependencies:

   .. code-block:: bash

      git clone https://github.com/tum-ens/pylovo.git
      cd pylovo
      uv sync
      source .venv/bin/activate

3. Configure pylovo:

   a) Copy ``.env.example`` to ``.env`` and align credentials with InfDB setup in environment file.
   b) Optionally adjust grid generation parameters in ``config/config_generation.yaml``.

4. Initialize pylovo database:

   a) Run ``pylovo-setup`` to create database schema and import some data (osm transformer locations, municipal register data, equipment data, ...)

   .. code-block:: bash

      pylovo-setup

   b) Run ``pylovo-generate`` with region arguments (--plz or --ags) to create single or multiple synthetic LV distribution grids, e.g.:

   .. code-block:: bash

      # single ags code
      pylovo-generate --ags 09162000
      # multiple postal codes
      pylovo-generate --plz 80803 80802 80801

**Note**: All ``pylovo-*`` commands must be run with the virtual environment activated (``source .venv/bin/activate``) or alternatively with ``uv run <command>``.

Scientific Background
------------------------------------------------------------------------

For detailed methodology, see: `Reveron Baecker et al. (2025): Generation of low-voltage synthetic grid data for energy system modeling with the pylovo tool <https://doi.org/10.1016/j.segan.2024.101617>`_

License
====================
| The code of this repository is licensed under the **MIT License** (MIT).
| See `LICENSE.txt <LICENSE.txt>`_ for rights and obligations.
| Copyright: `pylovo <https://github.com/tum-ens/pylovo/>`_ © TUM ENS | `MIT <LICENSE.txt>`_

Citation
====================
| If you use this code in a scientific publication, please cite the following publication:
| * Reveron Baecker et al. (2025): `Generation of low-voltage synthetic grid data for energy system modeling with the pylovo tool <https://doi.org/10.1016/j.segan.2024.101617>`_


.. |badge_license| image:: https://img.shields.io/github/license/tum-ens/pylovo
    :target: LICENSE.txt
    :alt: License

.. |badge_documentation| image:: https://readthedocs.org/projects/pylovo/badge/?version=latest
    :target: https://pylovo.readthedocs.io/en/main/?badge=main
    :alt: Documentation
