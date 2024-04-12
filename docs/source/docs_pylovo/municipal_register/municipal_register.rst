Municipal Register
===================

The area for grid generation that can be input by the user is the PLZ Code (Postleitzahl). For the import of the building
dataset the AGS (Amtlicher Gemeindeschlüssel) is needed (see :doc:`../../grid_generation/index`).
For the classification, the Regiostar class of that area needs to be provided (see :doc:`../../classification/index`).

The municipal register is created when setting up the database of the project by running :code:`main_constructor.py`
where the following function is called:

.. autofunction:: municipal_register.join_regiostar_gemeindeverz.create_municipal_register

The data is read by the following functions:

Gemeindeverzeichnis
--------------------

With the following functions the necessary population data is imported:

.. autofunction:: municipal_register.gemeindeverzeichnis.import_functions.import_plz_einwohner

.. autofunction:: municipal_register.gemeindeverzeichnis.import_functions.import_zuordnung_plz

Regiostar
----------

The Regiostar Data is imported like this:

.. autofunction:: municipal_register.regiostar.import_regiostar.import_regiostar
