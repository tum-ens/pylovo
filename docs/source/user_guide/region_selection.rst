Region selection
================

pylovo generates grids per **postcode area** (PLZ, *Postleitzahl*). A region is given either as
one or more PLZ or as one or more municipalities (AGS, *Amtlicher Gemeindeschlüssel*), which are
resolved to all postcodes of the municipality.

.. code-block:: bash

   uv run pylovo-generate --plz 80803                # one postcode
   uv run pylovo-generate --plz 80803 80802 80801    # several postcodes
   uv run pylovo-generate --ags 09162000             # all postcodes of Munich
   uv run pylovo-generate --ags 09162000 09161000    # several municipalities

PLZ and AGS are passed as integers, so leading zeros can be omitted (``--ags 9162000`` equals
``--ags 09162000``). ``--plz`` and ``--ags`` cannot be combined in one call.

How regions are resolved
------------------------

.. list-table::
   :header-rows: 1
   :widths: 22 39 39
   :class: fixed-table

   * - Input
     - InfDB mode
     - File-based mode
   * - ``--plz``
     - Used directly. The postcode polygon is taken from ``pylovo.postcode`` or fetched from
       InfDB; the buildings and streets are selected by their ``postcode`` column.
     - The PLZ must exist in ``pylovo.municipal_register``; its AGS select the building
       shapefiles to import.
   * - ``--ags``
     - All PLZ of the AGS in ``pylovo.municipal_register``.
     - Same, plus the import of the building shapefiles of the AGS.

The municipal register is filled by ``pylovo-setup`` from the Gemeindeverzeichnis
(``data/municipal_register/gemeindeverzeichnis``) and the RegioStaR table
(``data/municipal_register/regiostar``). A PLZ can belong to several municipalities and a
municipality has several PLZ; the register holds one row per (PLZ, AGS) pair.

.. note::

   Postcode areas and municipal boundaries do not match exactly. ``--ags`` generates complete
   postcode areas, which may extend beyond the municipality, and in the file-based mode only the
   buildings of the imported municipalities are available inside a postcode area.

Finding codes
-------------

.. code-block:: sql

   -- postcodes with a name
   SELECT plz, note FROM pylovo.postcode WHERE note ILIKE '%Aying%';

   -- municipalities and their postcodes
   SELECT ags, name_city, plz FROM pylovo.municipal_register
   WHERE name_city ILIKE 'Aying%' ORDER BY plz;

With InfDB, grids can only be generated where the InfDB processor has produced buildings and
streets for the postcode (``basedata.buildings.postcode`` and the street tables). A postcode
without streets stops with ``No ways found in remote DB intersecting the given PLZ geometry``;
without residential buildings with households the settlement type cannot be determined.
