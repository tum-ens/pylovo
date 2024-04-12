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

---
_Rest to delete?_

### Software preparation

The main script runs in Python, in addition you would need, unless you connect to a already constructed pylovo database:

1. [PostgreSQL]: the default database management system
2. [PostGIS]: extension for PostgreSQL
3. [pgRouting]: extension for PostgreSQL

[//]: # (5. The branch result_analysis presents the plot_result.py where according to pandapower result from step 3, the grid generation result will be analysed to multiple perspectives including:)

[//]: # ()

[//]: # (   * some general overviews of total numbers of transformers, loads, cable length, etc.;)

[//]: # (   * numerical statistics of each size of transformers;)

[//]: # (   * spatial distribution of transformers;)

[//]: # (   * load estimation of household; )

[//]: # (   * spatial detialed picture of a single distribution grid &#40;picked by random index&#41;;)

[//]: # ()

[//]: # (users can by commenting or uncommenting corresponding codes in plot_result.py to select the required plots.)


[virtual environment]: https://realpython.com/what-is-pip/#using-pip-in-a-python-virtual-environment

[PostgreSQL]: https://www.postgresql.org/

[PostGIS]: https://postgis.net/install/

[pgRouting]: https://pgrouting.org/

[osm2po]: https://osm2po.de/