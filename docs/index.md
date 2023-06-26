# pylovo (python tool for low-voltage distribution grid generation)

This tool provides a comprehensive public-data-based module to generate synthetic distribution grid for a
freely-selected research area. The main data input is the buildings, roads and transformers geographic data obtained
from OpenStreetMap, with additional auxiliary datasets including postal code area polygons (to identify and select
research areas), consumer categories (to estimate loading performances of different types of buildings and households)
and infrastructure parameters, etc. The result outputs a feasible solution of aggregated distribution grid networks
within the research scope and automatically analyses the important grid statistics such that users to evaluate the
general grid properties.

## Install package

pylovo is developed as a Python package and can be installed with
`pip`, ideally by using a [virtual environment]. Required Python dependencies are installed in the background. Open up a
terminal, clone and install pylovo with:

``` sh
git clone git@gitlab.lrz.de:tum-ens/pylovo.git
```

Install the package with pip editable from local repository, developer option installs additional packages:

=== "User"

    ``` sh
    cd pylovo
    pip install -e .
    ```

=== "Developer"

    ``` sh
    cd pylovo
    pip install -e .[dev]
    ```

## Preamble

1. This tool focuses so far only on distribution grid level, the result is presented at a collection of local grids,
   where the transformer is connected to a constant external grid as the transmission level conjunction. Also for
   consumers, over-sized loads could be supplied directly by medium voltage level grid or equiped with individual
   transformers. Those loads are regarded as 'large consumers' and would not be presented in final result graphics (but
   will be analysed in statistics).
2. All the geographic data during the process is presented in PostGIS Geometry format where a Spatial Reference System
   should be defined to recognize the exact positions. By default, the SRID is selected as epsg:3035 since here the
   basic unit of geographic analysis is meter. When transforming to pandapower network, the geodata should be presented
   as lon/lat coordination which is epsg:4326 (automatically done in script), be careful of the two different reference
   system setting;
3. The various input data obtained from different public sources do not always align in terms of available scopes, for
   example, Open Street Map provides public construction data for a global scale, while the source of postal code areas
   are limited within Germany, also certain regulations and parameters could differ when the research comes to another
   country. Therefore, a default research scope so far for this model is Germany and will be extended to higher levels.
4. Due to complicated geographical situations, this tool so far CAN NOT guarantee 100% accuracy under all circumstances,
   please let us know what error occurs when you are using this model for your applications. We appreciate your
   comments!

## Repository structure

```
pylovo/
├── docs/ :       MkDocs Documentation
├── examples/ :   Example usage of the tool
│   ├── basic_grid_generation.ipynb
│   └── map_visualization_examples.ipynb
├── raw_data/ :   Part of the initially required data
├── results/ :    Folder for results
├── syngrid/ :    Python package
│   ├── __init__.py :            Package requirement
│   ├── config_data.py :         Configuration about Data IO
│   ├── config_version.py :      Version dependent configuration
│   ├── dump_functions.sql :     SQL functions to create
│   ├── GridGenerator.py :       Main class with grid generation methods
│   ├── pgReaderWriter.py :      Class for database communication
│   ├── SyngridDatabaseConstructor.py : Class to setup a syngrid database
│   └── utils.py :               Class independent helper functions
├── main_constructor.py :     Sample script to construct a syngrid database
├── main_grid_generation.py : Sample script to generate grid in a region
├── mkdocs.yml :              Documentation configuration
├── README.md :               Tool description
└── setup.py :                Python package configuration
```

---
_Rest to delete?_

### Software preparation

The main script runs in Python, in addition you would need, unless you connect to a already constructed pylovo database:

1. [PostgreSQL]: the default database management system
2. [PostGIS]: extension for PostgreSQL
3. [pgRouting]: extension for PostgreSQL

# Main workflow of the model (depricated)

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

[//]: # (5. The branch result_analysis presents the plot_result.py where according to pandapower result from step 3, the grid generation result will be analysed to multiple perspectives including:)

[//]: # ()

[//]: # (   * some general overviews of total numbers of transformers, loads, cable length, etc.;)

[//]: # (   * numerical statistics of each size of transformers;)

[//]: # (   * spatial distribution of transformers;)

[//]: # (   * load estimation of household; )

[//]: # (   * spatial detialed picture of a single distribution grid &#40;picked by random index&#41;;)

[//]: # ()

[//]: # (users can by commenting or uncommenting corresponding codes in plot_result.py to select the required plots.)

## Output data

1. A folder of all local distribution grid results will be created as .csv files, named as 'kcid{a}bcid{b}' (a,b are
   variables), such files can be easily read in python or input to pandapower;

2. A folder of all statistical analysis will be created, which the plotting process is based on;

3. Detailed building, road and transformer records will be saved in '_result' table in SQL;

4. All the graphics will also be saved as .png in a separate folder;

5. There are some auxiliary tables that result_analysis.py would use but not correlated with final results, you can
   either delete them and the process will generate them again, or keep them to save some computational effort for next
   run.

[virtual environment]: https://realpython.com/what-is-pip/#using-pip-in-a-python-virtual-environment

[PostgreSQL]: https://www.postgresql.org/

[PostGIS]: https://postgis.net/install/

[pgRouting]: https://pgrouting.org/

[osm2po]: https://osm2po.de/