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
# pylovo (python tool for low-voltage distribution grid generation)

This tool provides a comprehensive public-data-based module to generate synthetic low-voltage distribution grids for a
freely-selected research area. The main data input is the buildings, roads and transformers geographic data that are obtained
from OpenStreetMap, with additional auxiliary datasets including postal code area polygons (to identify and select
research areas), consumer categories (to estimate loading performances of different types of buildings and households)
and infrastructure parameters, etc. The result outputs a feasible solution of aggregated distribution grid networks
within the research scope and automatically analyses the important grid statistics to enable the user to evaluate the
general grid properties for the generated synthetic grids.

At the current state of the project the data is prepared for Bavaria, but will be extended to Germany.
Due to the large amount of data, external users need to setup a local posgresql database for the grid generation process.

A comprehensive documentation can be found under https://pylovo.readthedocs.io/en/latest/
A step by step tutorial to understand the product of this tool in jupyter notebooks can be found under examples.

License and Citation
====================
| The code of this repository is licensed under the **MIT License** (MIT).
| See `LICENSE.txt <LICENSE.txt>`_ for rights and obligations.
| See the *Cite this repository* function or `CITATION.cff <CITATION.cff>`_ for citation of this repository.
| Copyright: `pylovo <https://github.com/tum-ens/pylovo/>`_ © `TUM ENS`_ | `MIT <LICENSE.txt>`_


.. |badge_license| image:: https://img.shields.io/github/license/tum-ens/pylovo
    :target: LICENSE.txt
    :alt: License

.. |badge_documentation| image:: https://img.shields.io/github/actions/workflow/status/tum-ens/pylovo/gh-pages.yml?branch=production
    :target: https://tum-ens.github.io/pylovo/ 
    :alt: Documentation