Calculate Parameters for Grids
===============================

.. autoclass:: classification.parameter_calculation.GridParameters.GridParameters

has the attributes

.. code-block:: python

    self.version_id = VERSION_ID
    self.plz = plz
    self.bcid = bcid
    self.kcid = kcid
    self.no_connection_buses = None
    self.no_branches = None
    self.no_house_connections = None
    self.no_house_connections_per_branch = None
    self.no_households = None
    self.no_household_equ = None
    self.no_households_per_branch = None
    self.max_no_of_households_of_a_branch = None
    self.house_distance_km = None
    self.transformer_mva = None
    self.osm_trafo = None
    self.max_trafo_dis = None
    self.avg_trafo_dis = None
    self.cable_length_km = None
    self.cable_len_per_house = None
    self.max_power_mw = None
    self.simultaneous_peak_load_mw = None
    self.resistance = None
    self.reactance = None
    self.ratio = None
    self.vsw_per_branch = None
    self.max_vsw_of_a_branch = None

that are calculated in the function:

.. autofunction:: classification.parameter_calculation.GridParameters.GridParameters.calc_grid_parameters

To calculate the parameters for all grids in the classification version use:

.. autofunction:: classification.database_communication.perform_classification_tasks_for_multiple_plz.calculate_parameters_for_mulitple_plz

