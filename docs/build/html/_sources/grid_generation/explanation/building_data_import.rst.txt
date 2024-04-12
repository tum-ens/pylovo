Building Data Import
=====================

The building data is the basis for the grid generation as the building data contains geographical information as
well as the load that each consumer requires.

The building data is available at TUM as a dataset comprised of .shp files for residential :code:`res` and other
:code:`oth` buildings. The buildings are seperated by their Amtlicher Gemeindeschlüssel (AGS) a key for the
municipalities in Germany. The building dataset containing of files is to be unzipped and put into the directory
:code:`raw_data/buildings`.

The files for Munich are thus named like this: :code:`Oth_9162000` and :code:`Res_9162000`.

The task of importing the building data before further Grid Generation steps is handled by

.. autofunction:: raw_data.import_building_data.import_buildings_for_single_plz

Remark: The mapping of AGS with PLZ is not always unique. This might lead to unexpected building data import.

In this example, the PLZ for grid generation only overlapped with the AGS that buildings were imported in the most upper
part of the PLZ area.

.. image:: ../../images/grid_generation/ags_plz_mismatch.png
    :width: 500
    :alt: Default view

The imported buildings can be inspected using the QGIS visualisation :doc:`../../visualisation/qgis/qgis` in the
:code:`raw_data` tab
