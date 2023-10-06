Main workflow of the model:
===========================

#. The research scope identification is done by either manually setting the plz code in GridGeneration.py main script or
   an automatic search according to the administrative name of the district.

#. Run GridGeneration.py script, and the process will be proceeded to:

    * extract correlated buildings, roads and transformers involved in the selected area;
    * estimate the buildings' peak load and remove too large consumers (connected directly to medium-voltage grid)
    * connect the buildings and transformers to the roads and analyse the network topology, remove isolated components;
    * according to edge-distance matrix, assign transformers with corresponding neighboring buildings, regarding cable
      length limit and capacity limit;
    * the remaining unsupplied buildings are subdivided into local distribution grids by hierarchical clustering, with
      timely simultaneous peak load validation to determine proper cluster sizes;
    * the optimal positions of manually grouped distribution grids are determined by minimal power-distance algorithm,
      aiming to minimize the network voltage band, energy losses on conductors and with shorter total cable length;

#. At the end of GridGeneration.py process, the basic nodal elements of all the local distribution grids have been
   determined. The installation of cables are determined in Cable_installation.py with support of pandapower;

#. The timely power flow calculation shall afterwards be conducted with random assignment of household load profiles,
   the default validation period is one year from 00:00:00 01.01.2019 - 24:00:00 31.12.2019 with time step of 15
   minutes, where users can freely shorten or prolong the period or increase the time step by modifying default
   parameters and run power_flow_calculation.py

#. In branch result_analysis presents the plot_result.py where according to pandapower result from step 3, the grid
   generation result will be analysed to multiple perspectives including:

    * some general overviews of total numbers of transformers, loads, cable length, etc.;
    * numerical statistics of each size of transformers;
    * spatial distribution of transformers;
    * load estimation of household;
    * spatial detailed picture of a single distribution grid (picked by random index);

users can by commenting or uncommenting corresponding codes in plot_result.py to select the required plots.