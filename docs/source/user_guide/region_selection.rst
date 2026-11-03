Region selection
================

pylovo generates grids per **postcode area** (PLZ, *Postleitzahl*). A region is given as one or
more PLZ, as one or more municipalities (AGS, *Amtlicher Gemeindeschlüssel*), which are resolved
to all postcodes of the municipality, or as every postcode the InfDB has buildings for, optionally
only in some federal states.

.. code-block:: bash

   uv run pylovo-generate --plz 80803                # one postcode
   uv run pylovo-generate --plz 80803 80802 80801    # several postcodes
   uv run pylovo-generate --ags 09162000             # all postcodes of Munich
   uv run pylovo-generate --ags 09162000 09161000    # several municipalities
   uv run pylovo-generate --all                      # every postcode with buildings in the InfDB
   uv run pylovo-generate --all by nrw               # those with buildings in Bavaria or NRW

PLZ and AGS are passed as integers, so leading zeros can be omitted (``--ags 9162000`` equals
``--ags 09162000``). ``--plz``, ``--ags`` and ``--all`` cannot be combined in one call. A federal
state of ``--all`` is its ISO 3166-2:DE code in any case (``bw``, ``by``, ``be``, ``bb``, ``hb``,
``hh``, ``he``, ``mv``, ``ni``, ``nw`` or ``nrw``, ``rp``, ``sl``, ``sn``, ``st``, ``sh``, ``th``)
or its key, the first two digits of the AGS (``9`` or ``09`` for Bavaria).

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
   * - ``--all``
     - Every ``postcode`` of the buildings in ``INFDB_SOURCE_SCHEMA.buildings`` (with
       ``USE_EXTERNAL_BASEDATA`` those of the external InfDB), the PLZ with the most buildings
       first. Buildings without a postcode belong to no PLZ. With states, only the buildings whose
       ``gemeindeschluessel`` starts with a state key count, so a PLZ across a state border
       belongs to both states.
     - Not available: pass ``--plz`` or ``--ags``.

The municipal register is filled by ``pylovo-setup`` from the Gemeindeverzeichnis
(``municipal_register/gemeindeverzeichnis`` of the data directory) and the RegioStaR table
(``municipal_register/regiostar``). The data directory is ``data/``, or ``PYLOVO_DATA_DIR``
(:ref:`env-variables`). A PLZ can belong to several municipalities and a
municipality has several PLZ; the register holds one row per (PLZ, AGS) pair.

The RegioStaR table is the reference of 2020 (``ReferenzGebietsstand2020``). A municipality that was
merged or renamed since has no class there: five (PLZ, AGS) pairs of the shipped files. They stay in the
register, so ``--plz`` and ``--ags`` resolve them, with empty (NULL) ``fed_state``, ``regio7`` and
``regio5`` and the municipality name of the PLZ assignment file. The classification does not draw
PLZ without a class (:doc:`classification`).

.. note::

   Postcode areas and municipal boundaries do not match exactly. ``--ags`` generates complete
   postcode areas, which may extend beyond the municipality, and in the file-based mode only the
   buildings of the imported municipalities are available inside a postcode area. In the same
   way, ``--all`` takes the extent of the InfDB: a postcode area cut by the edge of the imported
   region is generated with the buildings inside it.

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
