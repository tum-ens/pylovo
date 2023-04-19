# pylovo (python tool for low-voltage distribution grid generation)

This tool provides a comprehensive public-data-based module to generate synthetic distribution grid for a freely-selected research area. The main data input is the buildings, roads and transformers geographic data obtained from OpenStreetMap, with additional auxiliary datasets including postal code area polygons (to identify and select research areas), consumer categories (to estimate loading performances of different types of buildings and households) and infrastructure parameters, etc. The result outputs a feasible solution of aggregated distribution grid networks within the research scope and automatically analyse the important grid statistics such that users to evaluate the general grid properties.  

## Default settings:
1. This tool focuses so far only on distribution grid level, the result is presented at a collection of local grids, where the transformer is connected to a constant external grid as the transmission level conjunction. Also for consumers, over-sized loads could be supplied directly by medium voltage level grid or equiped with individual transformers. Those loads are regarded as 'large consumers' and would not be presented in final result graphics (but will be analysed in statistics).

2. All the geographic data during the process is presented in PostGIS Geometry format where a Spatial Reference System should be defined to recognize the exact positions. By default, the SRID is selected as epsg:3035 since here the basic unit of geographic analysis is meter. When transforming to pandapower network, the geodata should be presented as lon/lat coordination which is epsg:4326 (automatically done in script), be careful of the two different reference system setting;

3. The various input data obtained from different public sources do not always align in terms of available scopes, for example, Open Street Map provides public construction data for a global scale, while the source of postal code areas are limited within Germany, also certain regulations and parameters could differ when the research comes to another country. Therefore, a default research scope so far for this model is Germany and will be extended to higher levels.

4. Due to complicated geographical situations, this tool so far CAN NOT guarantee 100% accuracy under all circumstances, please let us know what error occurs when you are using this model for your applications. We appreciate your comments!


### Software preparation:
The main script runs in Python, in addition you would need:
1. PostgreSQL: default database;
2. PostGIS: extension for PostgreSQL, https://postgis.net/install/;
3. Pandapower: extension of Python where final grid result is displayed;

# Main workflow of the model:

1. The research scope identification is done by either manually setting the plz code in GridGeneration.py main script or an automatic search according to the administrative name of the district.
       
2. Run GridGeneration.py script, and the process will be proceeded to:

   * extract correlated buildings, roads and transformers involved in the selected area;
   * estimate the buildings' peak load and remove too large consumers (connected directly to medium-voltage grid)
   * connect the buildings and transformers to the roads and analyse the network topology, remove isolated components;
   * according to edge-distance matrix, assign transformers with corresponding neighboring buildings, regarding cable length limit and capacity limit;
   * the remaining unsupplied buildings are subdivided into local distribution grids by hierarchical clustering, with timely simultaneous peak load validation to determine proper cluster sizes;
   * the optimal positions of manually grouped distribution grids are determined by minimal power-distance algorithm, aiming to minimize the network voltage band, energy losses on conductors and with shorter total cable length;

3. At the end of GridGeneration.py process, the basic nodal elements of all the local distribution grids have been determined. The installation of cables are determined in Cable_installation.py with support of pandapower; 

4. The timely power flow calculation shall afterwards be conducted with random assignment of household load profiles, the default validation period is one year from 00:00:00 01.01.2019 - 24:00:00 31.12.2019 with time step of 15 minutes, where users can freely shorten or prolong the period or increase the time step by modifying defaulte parameters and run power_flow_calculation.py

5. In branch result_analysis presents the plot_result.py where according to pandapower result from step 3, the grid generation result will be analysed to multiple perspectives including:

   * some general overviews of total numbers of transformers, loads, cable length, etc.;
   * numerical statistics of each size of transformers;
   * spatial distribution of transformers;
   * load estimation of household; 
   * spatial detialed picture of a single distribution grid (picked by random index);

users can by commenting or uncommenting corresponding codes in plot_result.py to select the required plots.

## Outpus data:

1. A folder of all local distribution grid results will be created as .csv files, named as 'kcid{a}bcid{b}' (a,b are variables), such files can be easily read in python or input to pandapower;

2. A folder of all statistical analysis will be created, which the plotting process is based on;

3. Detailed building, road and transformer records will be saves in '_result' table in SQL;

4. All the graphics will also be saved as .png in a separate folder;

*5. There are some auxiliary tables that result_analysis.py would use but not correlated with final results, you can either delete them and the process will generate them again, or keep them to save some computational effort for next run.  
