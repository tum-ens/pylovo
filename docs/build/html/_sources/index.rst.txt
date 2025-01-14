.. pylovo gui documentation master file, created by sphinx-quickstart on Wed Jul 12 12:49:48 2023

pylovo
******************************************************************
python tool for low-voltage distribution grid generation
===========================================================
This tool provides a comprehensive public-data-based module to generate synthetic low-voltage distribution grids for a
freely-selected research area. The main data input is the buildings, roads and transformers geographic data that are obtained
from OpenStreetMap, with additional auxiliary datasets including postal code area polygons (to identify and select
research areas), consumer categories (to estimate loading performances of different types of buildings and households)
and infrastructure parameters, etc. The result outputs a feasible solution of aggregated distribution grid networks
within the research scope and automatically analyses the important grid statistics to enable the user to evaluate the
general grid properties for the generated synthetic grids.

At the current state of the project the data is prepared for Bavaria, but will be extended to Germany.
Due to the large amount of data, external users need to setup a local posgresql database for the grid generation process.
All the geographic data during the process is presented in PostGIS Geometry format where a Spatial Reference System
should be defined to recognize the exact positions. For the Spatial Reference System by default, the SRID is selected as epsg:3035 since here the
basic unit of geographic analysis is meter. When transforming to pandapower network, the geodata should be presented
as lon/lat coordination which is epsg:4326 (automatically done in script), be careful of the two different reference
system settings;

A comprehensive documentation can be found under https://pylovo.readthedocs.io/en/latest/
A step by step tutorial to understand the product of this tool in jupyter notebooks can be found under examples.

In this documentation you can find instructions and information on:


* how to install pylovo in :doc:`installation`.
* how to generate grids in :doc:`grid_generation`.

.. image:: images/grid_generation/grid_generation_part_forchheim.png
    :width: 300
    :alt: Default view


* how the grids are generated in :doc:`grid_generation/explanation/grid_generation_process`.
* how to create grid classes and representative grids in :doc:`classification/index`.

.. image:: images/classification/classification_3grids.png
    :width: 600
    :alt: Default view


* how to visualise your results in :doc:`visualisation/index`.

.. image:: images/visualisation/contextily.png
    :width: 300
    :alt: Default view

Contents
===============

.. toctree::
    :maxdepth: 2

    installation/installation
    grid_generation/index
    classification/index
    visualisation/index
    docs_sphinx/index

Legal Notice
==========================
`MIT License <https://opensource.org/license/MIT>`_ , Copyright (C) 2023-2024 Beneharo Reveron Baecker

Acknowledgement
==========================
The development of this software has been supported by contributions of the following persons: Soner Candas, Deniz Tepe,
Tong Ye, Daniel Baur, Julian Zimmer and Berkay Olgun.