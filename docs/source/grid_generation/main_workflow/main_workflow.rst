Main workflow
*************

Main workflow of the model
===========================

#. The research scope identification is done by either manually setting the plz code in grid generator scripts or
   an automatic search according to the administrative name of the district.

#. Run grid generator script, and the process will be proceeded to:

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

Main workflow of the model (depricated)
=======================================
1. The research scope identification is done by either manually setting the plz code in GridGeneration.py main script or
   an automatic search according to the administrative name of the district.

2. Run GridGeneration.py script, and the process will be proceeded to:

    - extract correlated buildings, roads and transformers involved in the selected area
    - estimate the buildings' peak load and remove too large consumers (connected directly to medium-voltage grid)
    - connect the buildings and transformers to the roads and analyse the network topology, remove isolated components
    - according to edge-distance matrix, assign transformers with corresponding neighbouring buildings, regarding cable
      length limit and capacity limit
    - the remaining unsupplied buildings are subdivided into local distribution grids by hierarchical clustering, with
      timely simultaneous peak load validation to determine proper cluster sizes
    - the optimal positions of manually grouped distribution grids are determined by a minimal power-distance algorithm,
      aiming to minimize the network voltage band, energy losses on conductors and total cable length

3. At the end of GridGeneration.py process, the basic nodal elements of all the local distribution grids have been
   determined. The installation of cables are determined in Cable_installation.py with support of pandapower;

4. The timely power flow calculation shall afterward be conducted with random assignment of household load profiles, the
   default validation period is one year from 00:00:00 01.01.2019 - 24:00:00 31.12.2019 with time step of 15 minutes,
   where users can freely shorten or prolong the period or increase the time step by modifying defaulte parameters and
   run power_flow_calculation.py

5. Some example vizualizations with an overview on total numbers and statistics of transformers, loads, cable length,
   etc. on one side and a detialed spatial picture of chosen distribution grids on the other side are in development and
   will be provided in a jupyter notebook in the examples directory.

Software preparation
=====================
The main script runs in Python, in addition you would need:

1. PostgreSQL: default database;

Output data
============
1. A folder of all local distribution grid results will be created (if you toggle the default 
   SAVE_GRID_FOLDER in config, it is default False so the results only get saved to the database) 
   as .csv files, named as 'kcid{a}bcid{b}' (a,b are
   variables), such files can be easily read in python or input to pandapower;

2. A folder of all statistical analysis will be created, which the plotting process is based on;

3. Detailed building, road and transformer records will be saves in '_result' table in SQL;

4. All the graphics will also be saved as .png in a separate folder;

*5. There are some auxiliary tables that result_analysis.py would use but not correlated with final results, you can
either delete them and the process will generate them again, or keep them to save some computational effort for next
run.
