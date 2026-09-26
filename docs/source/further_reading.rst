Further reading
===============

pylovo and its data foundation
------------------------------

* Reveron Baecker et al. (2025), `Generation of low-voltage synthetic grid data for energy
  system modeling with the pylovo tool <https://doi.org/10.1016/j.segan.2024.101617>`_. The
  published pylovo methodology; please cite this paper when using pylovo in scientific work.
* B. Reveron Baecker, K. Kalkan, P. Buchenberg, A. Mohapatra and T. Hamacher (2025),
  `A Reproducible Open-Data Pipeline for Synthetic Low-Voltage Grid Generation in Germany
  <https://doi.org/10.30420/566656008>`_, IEEE Power and Energy Student Summit. Describes the
  InfDB preprocessing that supplies pylovo's newer building and street inputs.
* Buchenberg et al. (2026), `InfDB: An Open Source Energy and Infrastructure Data Ecosystem for
  Modeling and Planning <https://doi.org/10.21105/joss.10458>`_, *Journal of Open Source
  Software* 11(122), 10458. The input data framework used in the recommended pylovo setup.
* `IEEE Xplore publication 11443094 <https://ieeexplore.ieee.org/document/11443094>`_.

Surrogate modelling
-------------------

* A. Mohapatra, M. Hock, B. Reveron Baecker and T. Hamacher (2025), `Surrogate Framework for
  Energy System Modeling <https://doi.org/10.1109/PowerTech59965.2025.11180532>`_, IEEE Kiel
  PowerTech, pp. 1–6.
* G. Pjetri, A. Mohapatra, B. Reveron Baecker, M. Hock and T. Hamacher (2025), `Graph Neural
  Network Surrogates for Energy System Modeling
  <https://doi.org/10.1109/ISGTEurope64741.2025.11305443>`_, IEEE PES ISGT Europe.
* E. F. Hanser, A. Mohapatra, B. Reveron Baecker and T. Hamacher (2027), `Deep learning
  surrogates for low-voltage grid planning <https://doi.org/10.1016/j.epsr.2026.113777>`_,
  *Electric Power Systems Research* 264, 113777.

Related projects
----------------

* `InfDB <https://github.com/tum-ens/InfDB>`_ -- harmonised building, street and postcode inputs.
* `pandapower <https://www.pandapower.org/>`_ -- the default electrical modelling backend.
* `pgRouting <https://pgrouting.org/>`_ and `PostGIS <https://postgis.net/>`_ -- routing and
  spatial functions used by pylovo.

Changelog
---------

Changes between releases are listed in
`CHANGELOG.md <https://github.com/tum-ens/pylovo/blob/main/CHANGELOG.md>`_.
