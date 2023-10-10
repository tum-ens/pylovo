.. pylovo gui documentation master file, created by sphinx-quickstart on Wed Jul 12 12:49:48 2023

.. toctree::
    :maxdepth: 2
    
    docs_pylovo/installation
    docs_gui/index


pylovo (python tool for low-voltage distribution grid generation)
******************************************************************

| Our climate goals and current political events more than ever demonstrate the need for transitioning towards locally sourced, 
  environmentally less harmful heating solutions. 
  This transition is not easily made, however: building structure, existing heating and availability of renewable electricity vary wildly in 
  different parts of the country and often even within the same cities. Due to this heterogeneity, improvements such as housing renovation 
  or new heating technologies need to be tailored to specific districts and their individual needs. At the same time, it is important to 
  keep a view of the big picture, to avoid different districts working at cross-purposes.

| To look at these problems the research association “Energy - Sector Coupling and Micro-Grids", or "STROM" for short, 
  is developing a digital automatized energy supply planning tool to rapidly advance the transformation of the energy system. 
  The tool will be able to use available local data including renewable resource potentials, distribution grid topology and energy demand 
  to help with the planning of power and heat supply structures. The tool is built on two central components: syngrid, a module for 
  generating synthetic distribution grids for a free-selected research area as well as the optimization framework urbs, a linear programming 
  model for multi-commodity energy systems with a focus on optimal storage sizing and use.

| While the individual components of this tool had already existed prior to the start of this IDP, it was the student’s responsibility to 
  combine them in a graphical application that allows a user to quickly visualize different grids and set parameters as well as results 
  of the optimization of said grids.


Preamble
========

This tool provides a comprehensive public-data-based module to generate synthetic distribution grid for a
freely-selected research area. The main data input is the buildings, roads and transformers geographic data obtained
from OpenStreetMap, with additional auxiliary datasets including postal code area polygons (to identify and select
research areas), consumer categories (to estimate loading performances of different types of buildings and households)
and infrastructure parameters, etc. The result outputs a feasible solution of aggregated distribution grid networks
within the research scope and automatically analyses the important grid statistics such that users to evaluate the
general grid properties.

#. This tool focuses far only on distribution grid level. The result is presented at a collection of local grids
   where the transformer is connected to a constant external grid as the transmission level conjunction. For
   consumers, over-sized loads could be supplied directly by medium voltage level grid or equiped with individual
   transformers. Those loads are regarded as 'large consumers' and would not be presented in final result graphics (but
   will be analysed in statistics).
#. All the geographic data during the process is presented in PostGIS Geometry format where a Spatial Reference System
   should be defined to recognize the exact positions. By default, the SRID is selected as epsg:3035 since here the
   basic unit of geographic analysis is meter. When transforming to pandapower network, the geodata should be presented
   as lon/lat coordination which is epsg:4326 (automatically done in script), be careful of the two different reference
   system settings;
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
   ├───docs                                        contains sphinx documentation files
   │   ├───build                                   contains html files after local doc generation 
   │   └───source                                  contains documentation source files
   │       ├───docs_gui                            describes usage, API of the gui
   │       ├───docs_pylovo                         describes usage of pylovo 
   │       ├───docs_sphinx                         describes setup of documentation
   │       ├───images                              
   │       ├───_static
   │       └───_templates
   ├───gui
   │   ├───fig
   │   ├───IDP_MapTool_Flask
   │   │   ├───instance
   │   │   ├───maptool                             main gui code folder
   │   │   │   ├───network_editor                  js code for network editor window
   │   │   │   ├───postcode_editor                 js code for postcode editor window
   │   │   │   ├───static
   │   │   │   ├───templates
   │   │   │   ├───urbs_editor                     js code for urbs setup window
   │   │   │   ├───urbs_results                    js code for urbs results window
   │   │   │   ├───z_feature_jsons                 contains json files defining variables, their types, default values
   │   │   │   │   ├───pandapower_network_features 
   │   │   │   │   └───urbs_setup_features
   │   │   ├───pandapower2urbs                     code converting pdp net into urbs input file
   │   │   ├───pandapower2urbs_dataset_template    excel files containing default values for urbs setup
   │   │   └───results                             
   │   └───urbs                                    code for the urbs optimization model
   │       └───urbs_result                         result data of successful urbs runs
   ├── examples/ :                                 Example usage of the tool
   │   ├── basic_grid_generation.ipynb
   │   └── map_visualization_examples.ipynb
   ├── raw_data/ :                                 Part of the initially required data
   ├── results/ :                                  Folder for results
   ├── syngrid/ :                                  Python package
   │   ├── __init__.py :                           Package requirement
   │   ├── config_data.py :                        Configuration about Data IO
   │   ├── config_version.py :                     Version dependent configuration
   │   ├── dump_functions.sql :                    SQL functions to create
   │   ├── GridGenerator.py :                      Main class with grid generation methods
   │   ├── pgReaderWriter.py :                     Class for database communication
   │   ├── SyngridDatabaseConstructor.py :         Class to setup a syngrid database
   │   └── utils.py :                              Class independent helper functions
   ├── main_constructor.py :                       Sample script to construct a syngrid database
   ├── main_grid_generation.py :                   Sample script to generate grid in a region
   └── setup.py :                                  Python package configuration
