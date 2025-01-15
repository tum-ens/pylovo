==========
pylovo
==========

**A repo to generate synthetic low-voltage distribution grids based on open data.**

.. list-table::
   :widths: auto

   * - License
     - |badge_license|
   * - Documentation
     - |badge_documentation|

.. contents::
    :depth: 2
    :local:
    :backlinks: top

Introduction
============
**pylovo (PYthon tool for LOw-VOltage distribution grid generation)**

This tool provides a comprehensive public-data-based module to generate synthetic low-voltage distribution grids for a
freely-selected research area. The main data input is the buildings, roads and transformers geographic data that are obtained
from OpenStreetMap, with additional auxiliary datasets including postal code area polygons (to identify and select
research areas), consumer categories (to estimate loading performances of different types of buildings and households)
and infrastructure parameters, etc. The result outputs a feasible solution of aggregated distribution grid networks
within the research scope and can automatically analyse the important grid statistics to enable the user to evaluate the
general grid properties for the generated synthetic grids.

At the current state of the project the data is prepared for Bavaria, but will be extended to Germany.
Due to the large amount of data, external users need to setup a local PosgreSQL database for the grid generation process.
A step by step tutorial to understand the product of this tool can be found in the notebook_tutorials directory.

License
====================
| The code of this repository is licensed under the **MIT License** (MIT).
| See `LICENSE.txt <LICENSE.txt>`_ for rights and obligations.
| Copyright: `pylovo <https://github.com/tum-ens/pylovo/>`_ © `TUM ENS`_ | `MIT <LICENSE.txt>`_

Citation
====================
| If you use this code in a scientific publication, please cite the following publication:
* Reveron Baecker et al. (2025): `Generation of low-voltage synthetic grid data for energy system modeling with the pylovo tool <https://doi.org/10.1016/j.segan.2024.101617>`_


.. |badge_license| image:: https://img.shields.io/github/license/tum-ens/pylovo
    :target: LICENSE.txt
    :alt: License

.. |badge_documentation| image:: https://readthedocs.org/projects/pylovo/badge/?version=latest
    :target: https://pylovo.readthedocs.io/en/main/?badge=main
    :alt: Documentation