# Advanced installation - Database construction

If you want to connect to the existing database, go
to [Advanced Installation/Outside the VM][outside-the-vm-from-ssh-client-aka-your-own-computer]. Follow the instructions
below, only if you want to create a new database for pylovo. Make sure you have the required raw data (! link to
reuqired input data description).

Initial steps to create a PostrgeSQL database on ENS virtual machine and connect to the db from local computer are
listed below.

## Install postgresql on linux

Since arbitrary package installation can be problematic due to the user rights, postgresql can be installed inside a
conda environment. Following instruction should be sufficient to create and run a database.

[https://gist.github.com/gwangjinkim/f13bf596fefa7db7d31c22efd1627c7a](https://gist.github.com/gwangjinkim/f13bf596fefa7db7d31c22efd1627c7a)

## Postgis & PGRouting

Postgis extension has to be installed via conda as well. The extension then can only be created by the base user

```bash
conda install -c conda-forge postgis
conda install -c conda-forge pgrouting
# TODO hstore?
```

## Access db

### Inside the VM

Within the vm the database is accessible with any tool like psql or from Python via engines.

Basic inspection of db can be done within the bash with psql

```bash
# psql -d db_name -U username 
psql -d pylovo_db -U pylovo
```

### Outside the VM (from ssh client a.k.a. your own computer)

The connection to the VM is described at the wiki already. However, basic connection forwards you to the bash. But we
want to connect directly to the db running on the port 5432. This is possible, when we map this port at vm to a port at
the localhost, when we build up the ssh connection.
The [StackOverflow answer](https://stackoverflow.com/questions/16835761/postgresql-via-ssh-tunnel) suggest the
following:

``` sh
ssh -L 1111:localhost:5432 user@remote.example.com
```

This way the port 1111 on the localhost listens to the database in the VM. Now you can connect to the port localhost:
1111 from any tool, e.g. pgAdmin. On pgAdmin, you have to create a new server with the aforementioned connection
configuration.

---

* In case you are not connected to the eduroam, try to use a [VPN connection](https://doku.lrz.de/display/PUBLIC/VPN)

## Create SQL functions

Prewritten SQL functions must be created for once, when the database is created. Run the file
_syngrid/dump_functions.sql_:

``` sh
    psql -d syngrid_db -a -f "syngrid/dump_functions.sql"
```

## Load raw data to the database

pylovo requires the correct table structure and input data already loaded into the database. Make sure that you have the
raw data files (link to input data description) and paths configured in _config\_data.py_

Afterwards, the ETL process can be executed as:

``` python
python main_constructor.py
```

<!-- Remark Julian: I made some changes. Buildings and transformers will be imported directly from the scripts main_grid_generation and process_trafos. -->

## Input data model (TODO add required columns, data type, description)

The minimum data model is described below:

* res
* oth
* betriebsmittel
* postcode
* ways (ways from OSM must be preprocessed (link to ways preprocessing))
* consumer_categories?
* transformers (
  optional) <!-- Remark Julian: This is not optional, you need to import some transformers or at least create the table, else GridGenerator will  throw an error.-->

### Preprocess ways from OSM data

The first steps follow the
workshop [https://workshop.pgrouting.org/2.6/en/chapters/prepare_data.html](https://workshop.pgrouting.org/2.6/en/chapters/prepare_data.html)

1. Download [osm2po]. Alternatively, you can try osm2pgrouting, but it's a bit more complicated for Windows. osm2po
   creates an SQL query. The generated table represents a graph and can be used with [pgrouting].
2. After extraction, edit the .config file.
    1. tileSize=x # Not necessary and without tiling recommended as long as there is enough RAM.
    2. Uncomment "ways" except for "ferry". Otherwise, these ways will be ignored.
    3. "Finalmask" determines the transportation modes intended for routing and only considers the appropriate ways. By
       default, it is set to "car," so footways, for example, will be ignored. Comment out the
       line `.default.wtr.finalMask = car`.
    4. Uncomment the line `postp.0.class = de.cm.osm2po.plugins.postp.PgRoutingWriter`.
       Otherwise, no SQL file will be created.
3. In the Command Line, navigate to the osm2po-5.3.6 folder. Run the main command to create the .sql file:

    ```sh
    java -Xmx1g -jar osm2po-core-5.3.6-signed.jar prefix=public \
     "C:\Users\path\to\osm\file\bayern_sorted.osm.pbf"
    ```

    - -Xmx1g: increases the maximum heap size for Java. 1GB was sufficient for Bayern.
    - prefix=public: choose any name
4. Navigate to the newly created "public" folder. If the .sql file exists, then most of the work is done. The commands
   inside should be executed easily.
   `psql -U postgres -d bayern_sgg -q -f .\public_2po_4pgr.sql`

### Preprocess transformers from OSM data (Optional)

1. Copy the code from `substations_bayern_query_overpass.txt` into the OSM API [overpass turbo]. The file is in
   the `raw_data` package.
   Run the script in overpass and export the results as a geojson. Name the file `substations_bayern.geojson` and save
   it in the `raw_data` package.

   The query searches for the keywords 'transformer' and 'substation' in the area 'Bayern'. Substations from the '
   Deutsche Bahn' as well as 'historic' and 'abandoned' substations are excluded. This yields around 22.000 results.
   More information on positions of transformers can be found on [OpenInfrastructureMap].
2. A second overpass query is to be performed. Repeat the previous steps for `shopping_mall_query_overpass.txt`.

   The query searches for all places that are tagged with keywords that indicate that transformers nearby do not belong
   to the LV grid. e.g. 'shopping malls' are likely directly connected to the MV grid.
   Other filters include land use of the oil industry like refineries, power plants e.g. solar fields, military training
   areas, landuse "rail" and "education" and large surface parking.

   Make any changes to the overpass queries that you see fit.

3. Open the `process_trafos.py` script. At the top there are some constants that can be changed. These are:
   AREA_THRESHOLD, MIN_DISTANCE_BETWEEN_TRAFOS,VOLTAGE_THRESHOLD and EPSG.

* The script imports the geojson files from 1. and 2. . It transforms the geodata according to the EPSG projection. This
  is needed to calculate distances and areas. Transformers can either be points or polygons.
* First, all trafos are deleted that are positioned on top of another trafo. This is often the case for 'Umspannwerke' (
  HV transformers) where transformers have been tagged multiple times.
* Secondly, all transformers larger than the AREA_THRESHOLD are deleted. LV transformers are either points or they have
  smaller dimensions.
* Thirdly, all transformers above the VOLTAGE_THRESHOLD are deleted. Note that many transformers are not tagged with any
  voltage. They are not deleted.
* Step four: One transformers that is in close proximity of multiple transformers is deleted. The distance must be below
  MIN_DISTANCE_BETWEEN_TRAFOS.
* Step 5: All trafos that are close to or at the same position as e.g. shopping malls (see 2.) are deleted.
* the results are written to `substations_bayern_processed.geojson`. The results can be inspected in the QGIS
  file `trafos`.
* The transformers are added to the database

[virtual environment]: https://realpython.com/what-is-pip/#using-pip-in-a-python-virtual-environment

[PostgreSQL]: https://www.postgresql.org/

[PostGIS]: https://postgis.net/install/

[osm2po]: https://osm2po.de/

[overpass turbo]: https://overpass-turbo.eu/

[OpenInfrastructureMap]: https://openinframap.org/#12.73/48.18894/11.58542
