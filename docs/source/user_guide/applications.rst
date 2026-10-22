Applications
============

Detailed low-voltage (LV) networks are needed to study changes in electricity demand and local
supply, but network operators' grid models are often unavailable outside their organisations.
pylovo provides geographically located, reproducible **synthetic** networks so that regional
studies can use more detail than a handful of generic feeder types. The grids represent plausible
planning models; their topology and equipment must not be assumed to reproduce a particular
operator's network. The original pylovo `methodology paper
<https://doi.org/10.1016/j.segan.2024.101617>`_ discusses the need for regional synthetic data.
The later `open-data pipeline paper <https://doi.org/10.30420/566656008>`_ explains how
more detailed building and street inputs are prepared in InfDB.

Planning studies
----------------

* **Scenario analysis:** assign time-dependent demand, rooftop photovoltaic generation, heat
  pumps or electric-vehicle charging to buildings, then examine voltage and loading with
  pandapower. The generated snapshot is a starting point; scenario profiles must be supplied
  separately (:doc:`analysing_grids`).
* **Grid expansion studies:** estimate where future loading or voltage constraints may arise and
  compare reinforcement choices across postcodes or municipalities. Transformer and cable
  inventories can support approximate material and cost studies when paired with suitable cost
  assumptions.
* **Spatial analysis:** connect grid results with building and regional attributes in PostGIS or
  QGIS to investigate how settlement structure and demand affect feeder length, capacity and
  losses (:doc:`exporting_visualising`).
* **Method development:** compare brownfield assumptions, greenfield placement or equipment
  settings across reproducible ``VERSION_ID`` values (:doc:`generating_grids`).

Surrogate models
----------------

Generated networks and their simulated operating points can also supply training examples for
surrogate models: a learned model approximates a more expensive optimisation or power-flow
calculation for rapid scenario screening. Useful labels may include transformer demand,
voltage, loading or reinforcement needs. Keep the data split by region or grid when testing
whether a model generalises to unseen networks, and validate promising results with an
engineering simulation. Related work includes surrogate frameworks for energy system modelling,
graph neural networks with network outputs, and deep learning for LV grid planning; see
:doc:`../further_reading`. These publications demonstrate possible modelling approaches;
they do not imply that pylovo ships a trained surrogate.
