Further reading
===============

pylovo and its data foundation
------------------------------

* Reveron Baecker et al. (2025), `Generation of low-voltage synthetic grid data for energy
  system modeling with the pylovo tool <https://doi.org/10.1016/j.segan.2024.101617>`_. The
  published pylovo methodology; please cite this paper when using pylovo in scientific work.
* B. Reveron Baecker, K. Kalkan, P. Buchenberg, A. Mohapatra and T. Hamacher (2025),
  `A Reproducible Open-Data Pipeline for Synthetic Low-Voltage Grid Generation in Germany
  <https://ieeexplore.ieee.org/document/11443094>`_, IEEE Power and Energy Student Summit.
  Describes the InfDB preprocessing that supplies pylovo's newer building and street inputs.
* Buchenberg et al. (2026), `InfDB: An Open Source Energy and Infrastructure Data Ecosystem for
  Modeling and Planning <https://doi.org/10.21105/joss.10458>`_, *Journal of Open Source
  Software* 11(122), 10458. The input data framework used in the recommended pylovo setup.

Planning assumptions
--------------------

* S. Niederle, D. J. Storch, C. J. Steinhart, C. Gutzmann, M. Kreißl, M. Günther, R. Tonkoski,
  R. Witzmann and M. Finkel (2026), *Ermittlung des Netzausbaubedarfes im urbanen Verteilnetz durch
  Sektorenkopplung mittels vereinfachter Netzberechnung*, 19. Symposium Energieinnovation, Graz.
* Deutsche Energie-Agentur (dena) (2012), *Ausbau- und Innovationsbedarf der Stromverteilnetze in
  Deutschland bis 2030* (dena-Verteilnetzstudie), final report, Berlin.

Medium-voltage grids
--------------------

The method of the MV stage builds on the open-ring model of Ding0 (Amme et al. 2018), the HV/MV
substation filter of Hülk et al. (2017), the SimBench benchmark grids (Meinecke et al. 2020) and
the German planning principles for rural and urban distribution grids (Harnisch et al. 2016,
Wintzek et al. 2021). :ref:`mv-references` lists all sources.

Surrogate modelling
-------------------

A surrogate is a learned model that replaces an expensive step, here the energy system
optimisation and power flow of a low-voltage grid, so that many grids and scenarios can be screened
quickly. The training data is the bottleneck: it needs many grids with consistent topology,
buildings and time series, which are rarely available from real grid operators. pylovo supplies the
grids. The surrogate itself, the demand and generation time series and the optimisation come from
other tools; pylovo does not ship a trained model (see :doc:`user_guide/applications`).

* A. Mohapatra, M. Hock, B. Reveron Baecker and T. Hamacher (2025), `Surrogate Framework for
  Energy System Modeling <https://doi.org/10.1109/PowerTech59965.2025.11180532>`_, IEEE Kiel
  PowerTech, pp. 1–6. Per-grid surrogate of the optimisation of one real grid, trained on runs at
  several levels of distributed energy resources.
* G. Pjetri, A. Mohapatra, B. Reveron Baecker, M. Hock and T. Hamacher (2025), `Graph Neural
  Network Surrogates for Energy System Modeling
  <https://doi.org/10.1109/ISGTEurope64741.2025.11305443>`_, IEEE PES ISGT Europe. The same task
  with graph neural networks.
* E. F. Hanser, A. Mohapatra, B. Reveron Baecker and T. Hamacher (2027), `Deep learning
  surrogates for low-voltage grid planning <https://doi.org/10.1016/j.epsr.2026.113777>`_,
  *Electric Power Systems Research* 264, 113777. One transformer-based surrogate for any German
  low-voltage grid, trained on 1,200 pylovo grids. This is the paper to read for the use of pylovo
  data.
* H. Genena, A. Mohapatra, B. Reveron Baecker and T. Hamacher (2026), `Blending Surrogates for
  Energy System Modeling <https://doi.org/10.1109/PESGM58988.2026.11693709>`_, IEEE Power &
  Energy Society General Meeting (PESGM), Montreal, pp. 1–5.

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
