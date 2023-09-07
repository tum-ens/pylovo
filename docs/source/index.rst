.. pylovo gui documentation master file, created by
   sphinx-quickstart on Wed Jul 12 12:49:48 2023.
   You can adapt this file completely to your liking, but it should at least
   contain the root `toctree` directive.

pylovo (python tool for low-voltage distribution grid generation)
******************************************************************

This tool provides a comprehensive public-data-based module to generate synthetic distribution grid for a
freely-selected research area. The main data input is the buildings, roads and transformers geographic data obtained
from OpenStreetMap, with additional auxiliary datasets including postal code area polygons (to identify and select
research areas), consumer categories (to estimate loading performances of different types of buildings and households)
and infrastructure parameters, etc. The result outputs a feasible solution of aggregated distribution grid networks
within the research scope and automatically analyses the important grid statistics such that users to evaluate the
general grid properties.

Preamble
========

#. This tool focuses so far only on distribution grid level, the result is presented at a collection of local grids,
   where the transformer is connected to a constant external grid as the transmission level conjunction. Also for
   consumers, over-sized loads could be supplied directly by medium voltage level grid or equiped with individual
   transformers. Those loads are regarded as 'large consumers' and would not be presented in final result graphics (but
   will be analysed in statistics).
#. All the geographic data during the process is presented in PostGIS Geometry format where a Spatial Reference System
   should be defined to recognize the exact positions. By default, the SRID is selected as epsg:3035 since here the
   basic unit of geographic analysis is meter. When transforming to pandapower network, the geodata should be presented
   as lon/lat coordination which is epsg:4326 (automatically done in script), be careful of the two different reference
   system setting;
#. The various input data obtained from different public sources do not always align in terms of available scopes, for
   example, Open Street Map provides public construction data for a global scale, while the source of postal code areas
   are limited within Germany, also certain regulations and parameters could differ when the research comes to another
   country. Therefore, a default research scope so far for this model is Germany and will be extended to higher levels.
#. Due to complicated geographical situations, this tool so far CAN NOT guarantee 100% accuracy under all circumstances,
   please let us know what error occurs when you are using this model for your applications. We appreciate your
   comments!

Repository structure
=====================

::

    pylovo/
    ├── docs/ :       Sphinx Documentation
    ├── examples/ :   Example usage of the tool
    │   ├── basic_grid_generation.ipynb
    │   └── map_visualization_examples.ipynb
    ├── raw_data/ :   Part of the initially required data
    ├── results/ :    Folder for results
    ├── syngrid/ :    Python package
    │   ├── __init__.py :            Package requirement
    │   ├── config_data.py :         Configuration about Data IO
    │   ├── config_version.py :      Version dependent configuration
    │   ├── dump_functions.sql :     SQL functions to create
    │   ├── GridGenerator.py :       Main class with grid generation methods
    │   ├── pgReaderWriter.py :      Class for database communication
    │   ├── SyngridDatabaseConstructor.py : Class to setup a syngrid database
    │   └── utils.py :               Class independent helper functions
    ├── main_constructor.py :     Sample script to construct a syngrid database
    ├── main_grid_generation.py : Sample script to generate grid in a region
    ├── mkdocs.yml :              Documentation configuration
    ├── README.md :               Tool description
    └── setup.py :                Python package configuration

.. toctree::
    :maxdepth: 2

    pylovo/installation
    gui/index