Preprocess transformers from OSM data (Optional)
=================================================

#. Copy the code from :code:`substations_bayern_query_overpass.txt` into the OSM API `overpass turbo <https://overpass-turbo.eu/>`_. The file is in
   the :code:`raw_data` package.
   Run the script in overpass and export the results as a geojson. Name the file :code:`substations_bayern.geojson` and save
   it in the :code:`raw_data` package.

   The query searches for the keywords 'transformer' and 'substation' in the area 'Bayern'. Substations from the '
   Deutsche Bahn' as well as 'historic' and 'abandoned' substations are excluded. This yields around 22.000 results.
   More information on positions of transformers can be found on `OpenInfrastructureMap <https://openinframap.org/#12.73/48.18894/11.58542/>`_ .
#. A second overpass query is to be performed. Repeat the previous steps for :code:`shopping_mall_query_overpass.txt`.

   The query searches for all places that are tagged with keywords that indicate that transformers nearby do not belong
   to the LV grid. e.g. 'shopping malls' are likely directly connected to the MV grid.
   Other filters include land use of the oil industry like refineries, power plants e.g. solar fields, military training
   areas, landuse "rail" and "education" and large surface parking.

   Make any changes to the overpass queries that you see fit.

#. Open the :code:`process_trafos.py` script. At the top there are some constants that can be changed. These are:
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
* the results are written to :code:`substations_bayern_processed.geojson`. The results can be inspected in the QGIS
  file :code:`trafos`.
* The transformers are added to the database
