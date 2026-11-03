Non-residential loads
=====================

pylovo gives a residential building ``PEAK_LOAD_HOUSEHOLD`` per household. The load of a non-residential part of a
building (commercial, public, the shop of a mixed building) comes from its gross floor area,
``nonresidential_floor_area``. ``NONRESIDENTIAL_LOAD_MODEL`` (:doc:`../user_guide/configuration`) selects how:

``building_use`` (shipped, the versions before the load classes)
   One peak per m² of the use of the building, ``peak_load_per_m2`` of ``CONSUMER_CATEGORIES``: 79 W/m² for every
   Commercial and 29 W/m² for every Public floor area. Neither value has a documented source. Every hall, barn and
   warehouse classified Commercial gets the 79 W/m² of a supermarket with refrigeration, so many buildings exceed
   ``MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW`` and leave the LV grid as MV customers.

``load_class``
   A Commercial or Public part with a load class of the InfDB (``basedata.buildings.load_class``) gets the load of
   that class from ``LOAD_CLASSES``. The values per class come from the federal electricity benchmarks of
   non-residential buildings and from the BDEW standard load profiles (below). A part without a class keeps the W/m²
   of its use, or takes the load of the class ``UNCLASSIFIED_LOAD_CLASS``.

From annual electricity to the peak
-----------------------------------

Per class, ``LOAD_CLASSES`` holds the annual electricity *e* of a benchmark in kWh per m² of net floor area (NGF) and
a standard load profile. pylovo derives (:mod:`pylovo.load_classes`):

.. math::

   e_\mathrm{BGF} = e \cdot f_\mathrm{NGF}, \qquad
   p_\mathrm{coinc} = \frac{e_\mathrm{BGF}}{T_\mathrm{FLH}} \cdot 1000, \qquad
   p_\mathrm{own} = \frac{p_\mathrm{coinc}}{g_\infty}

* :math:`f_\mathrm{NGF}` = 0.85 is the net floor area per gross floor area. The benchmarks allow it for buildings
  without a category factor ("Umrechnung von der Bruttogeschossfläche über den Faktor 0,85", BMWi/BMUB 2015,
  Anlage 1); their factors per building category are 0.83 to 0.91.
* :math:`T_\mathrm{FLH}` are the full-load hours of the profile: annual energy over the maximum, so
  :math:`e_\mathrm{BGF} / T_\mathrm{FLH}` is the profile's maximum per m² in W.
* A standard load profile is the mean of many measured customers. Its maximum is therefore what one building adds
  to the peak of many buildings, :math:`p_\mathrm{coinc}`, not the own peak of one building.
* pylovo's coincidence law :math:`g(n) = g_\infty + (1 - g_\infty)\,n^{-3/4}` (:doc:`grid_dimensioning`) gives one
  building its installed load and many buildings :math:`g_\infty` times their installed load. The installed load
  per m² is therefore :math:`p_\mathrm{own} = p_\mathrm{coinc} / g_\infty`: then many buildings of a class reach
  the profile peak. :math:`g_\infty` is the ``sim_factor`` of the class's ``consumer_category`` (Commercial 0.5,
  Public 0.6), which becomes the ``nonresidential_use`` of the building; the class ``other`` keeps the use of the
  building.

Some benchmarks have a second value for large buildings (``large``). Above that net floor area of the
non-residential part (gross floor area x 0.85) the second value applies, as in the benchmark tables.

Classes and values
------------------

Benchmarks: BMWi/BMUB (2015), Tabelle 2.1 (buildings by the Bauwerkszuordnungskatalog, BWZK) and Tabelle 2.2 (by
use), column *Strom*, in kWh/(m² NGF · a). The electricity value covers at least cooling, ventilation, built-in
lighting and auxiliary energy, and with decentral hot water or electric supplementary heating also these (Nr. 2.1).
Own peak: Commercial / Public as listed.

.. list-table::
   :header-rows: 1
   :widths: 17 31 9 9 6 9 9
   :class: fixed-table

   * - Class
     - Benchmark (BMWi/BMUB 2015)
     - kWh/m² NGF
     - kWh/m² BGF
     - Profile
     - W/m² coincident
     - W/m² own peak
   * - ``office``
     - Tab. 2.2 Nr. 9.1 Bürogebäude, nur beheizt (the default of Tab. 2.2)
     - 35
     - 29.8
     - G1
     - 14.28
     - 28.6
   * - ``retail``
     - Tab. 2.2 Nr. 6.1 / 6.2 Handel Non-Food bis / über 300 m²
     - 45 / 60
     - 38.2 / 51.0
     - G4
     - 8.75 / 11.67
     - 17.5 / 23.3
   * - ``food_retail``
     - Tab. 2.2 Nr. 6.3 / 6.4 Handel Food bis / über 300 m²
     - 75 / 265
     - 63.8 / 225.2
     - G4
     - 14.59 / 51.54
     - 29.2 / 103.1
   * - ``hospitality``
     - Tab. 2.2 Nr. 1.3 Hotels mit 3 Sternen (hotels 50-65, restaurants 70-95)
     - 60
     - 51.0
     - G0
     - 12.17
     - 24.3
   * - ``education``
     - Tab. 2.1 BWZK 4100 Allgemeinbildende Schulen (other schools 15-20, universities 30-95)
     - 10
     - 8.5
     - G1
     - 4.08
     - 6.8
   * - ``health_care``
     - Tab. 2.1 BWZK 3000 Gebäude des Gesundheitswesens (acute hospitals BWZK 3200: 125)
     - 50
     - 42.5
     - G0
     - 10.14
     - 16.9
   * - ``sports``
     - Tab. 2.2 Nr. 5.1 Sporthallen
     - 35
     - 29.8
     - G2
     - 7.45
     - 12.4
   * - ``culture_leisure``
     - Tab. 2.2 Nr. 3.2 / 3.3 Theater, Saalbauten (= Tab. 2.1 BWZK 9120-9140)
     - 40
     - 34.0
     - G0
     - 8.11
     - 13.5
   * - ``worship``
     - Tab. 2.2 Nr. 3.4 Gemeindehäuser (no category for churches; the closest use)
     - 20
     - 17.0
     - G6
     - 5.10
     - 8.5
   * - ``public_administration``
     - Tab. 2.1 BWZK 1300 Verwaltungsgebäude bis / über 3 500 m²
     - 20 / 30
     - 17.0 / 25.5
     - G1
     - 8.16 / 12.24
     - 13.6 / 20.4
   * - ``production``
     - Tab. 2.1 BWZK 7000 Produktion, Werkstätten, Lager bis / über 3 500 m²
     - 20 / 65
     - 17.0 / 55.2
     - G0
     - 4.06 / 13.18
     - 8.1 / 26.4
   * - ``warehouse``
     - Tab. 2.2 Nr. 6.6 Geschlossene Lagerhäuser, Speditionen
     - 35
     - 29.8
     - G0
     - 7.10
     - 14.2
   * - ``agriculture``
     - Tab. 2.1 BWZK 7100 is not listed, so its main category BWZK 7000 bis / über 3 500 m²
     - 20 / 65
     - 17.0 / 55.2
     - L0
     - 4.08 / 13.27
     - 8.2 / 26.5
   * - ``other``
     - Tab. 2.2 Nr. 9.1 (Tab. 2.2: a use it does not cover counts as 9.1)
     - 35
     - 29.8
     - G0
     - 7.10
     - 14.2 / 11.8

The limits of 300 and 3 500 m² NGF are 353 and 4 118 m² of gross floor area. At 200 kW
(``MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW``) a building becomes an MV customer: an office from 7 005 m², a large
retail building from 8 569 m², a supermarket from 1 940 m², a large production hall from 7 586 m² and a school from
29 421 m² of gross floor area (79 W/m² before: 2 532 m² for every Commercial building).

Standard load profiles
~~~~~~~~~~~~~~~~~~~~~~

The BDEW (formerly VDEW) standard load profiles of 1999 (Meier et al. 1999) describe customers of up to about
100 MWh per year: G0 commerce in general, G1 workdays 8 to 18 h (offices, practices, workshops), G2 mainly evening
use (sports clubs, evening restaurants), G3 continuous operation, G4 shops and hairdressers, G5 bakeries, G6 weekend
operation, L0 to L2 agriculture. The full-load hours of ``load_profiles`` were computed from the typical days of the
profiles (``selp_series.csv`` of oemof demandlib, MIT licence) with the BDEW seasons and the public holidays as
Sundays, as the mean of the years 2015 to 2025 (``docs/scripts/slp_full_load_hours.py``; single years differ by less
than 1 %):

.. list-table::
   :header-rows: 1
   :class: fixed-table

   * - Profile
     - G0
     - G1
     - G2
     - G3
     - G4
     - G5
     - G6
     - L0
     - L1
     - L2
   * - Full-load hours
     - 4191
     - 2084
     - 3993
     - 6495
     - 4370
     - 3935
     - 3332
     - 4165
     - 3285
     - 4681

Cross-check
~~~~~~~~~~~

Planning guidelines give the power demand of whole buildings for sizing their supply (Siemens 2018, Tab. 2/3):
offices 30-50 W/m², shopping centres and department stores 30-60, hotels 30-60, schools 10-30, gym halls 15-30,
old people's homes 15-30, warehouses without cooling 2-20, production plants 30-80, hospitals 80-120 W/m². The own
peaks above are at or below the lower end of these ranges, as expected for measured consumption against planning
values with reserves. They are clearly lower for hospitals (``health_care`` holds the BWZK 3000 value, without acute
hospitals) and museums.

Coincidence
-----------

The own peak of one building and its contribution to aggregated peaks stay separate: the own peak
(``nonresidential_peak_load_in_kw``) decides the connection (MV-direct, connection level), pylovo's law the
contribution to a feeder, a station or the MV grid. The classes use the :math:`g_\infty` of their category; no
public source gives class-specific values for the coincidence between buildings. The coincidence factors of
planning guidelines (Siemens 2018: 0.6-0.8) combine the loads within one building and are not used.

.. _connection-levels:

Connection levels
-----------------

With ``CONNECTION_LEVEL_THRESHOLDS_KW`` every building gets a connection level on its own peak
(``buildings_result.connection_level``, ``mv_customers.connection_level``), following the voltage levels of demand in
eGon-data (up to 0.1 MW LV, 0.1-0.2 MW MV/LV station, 0.2-5.5 MW MV, 5.5-20 MW HV/MV station, from 20 MW HV):

.. list-table::
   :header-rows: 1
   :widths: 20 80
   :class: fixed-table

   * - Level
     - Rule (shipped example values)
   * - ``hv``
     - The non-residential part is MV-direct and its own peak is at least ``hv`` (20 MW).
   * - ``mv``
     - The non-residential part is MV-direct: above ``MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW`` (200 kW). The HV/MV
       station level of eGon-data (5.5-20 MW) counts as ``mv``. The residential part of the building stays in
       its LV grid.
   * - ``station_busbar``
     - The own peak of what stays in the LV grid is above ``station_busbar`` (100 kW): its households with
       pylovo's law plus its non-residential part.
   * - ``lv_feeder``
     - Otherwise.

The level is stored only: the LV planning connects a ``station_busbar`` building like any other. The MV stage keeps
its own rule for customers below ``customer_min_mv_kw`` (:ref:`mv-customers`).

InfDB load classes
------------------

``basedata.buildings.load_class`` holds the class of the non-residential part of a building (for a mixed building the
class of its non-residential part) from ALKIS function codes and OpenStreetMap tags; NULL means no evidence. pylovo
reads it only with ``NONRESIDENTIAL_LOAD_MODEL: load_class``; InfDB data without the column give every building
NULL (with a warning). A class applies to Commercial and Public parts. Unknown parts keep no load, and a class that
``LOAD_CLASSES`` does not list keeps the load of the use (with a warning). ``buildings_result.load_class`` stores the
InfDB class of every building.

A Commercial or Public part without a class keeps the W/m² of its use (79 or 29 W/m²) unless
``UNCLASSIFIED_LOAD_CLASS`` names a class of ``LOAD_CLASSES``; it then takes the load of that class, and its
``load_class`` stays NULL. ``other`` follows the benchmark itself: a use that Tab. 2.2 does not cover counts as Nr. 9.1
(offices, heated only). In the LoD2 test data of 2026 the generic ALKIS code 2000 (Gebäude für Wirtschaft oder Gewerbe)
without evidence made up two thirds of the parts without a class; the parts of that code with a class came to 19 W/m²
on average (14 W/m² as ``other``), the use to 79 W/m².

Versions
--------

The shipped configuration keeps the model ``building_use`` and no connection levels, so the versions generated before
the load classes continue unchanged: their snapshots lack ``load_calculation.nonresidential_load_model`` and
``connection_levels``, whose legacy values are ``building_use`` and ``null`` (:mod:`pylovo.version_snapshot`). A
version of the model ``load_class`` stores the classes with their inputs, derived values and sources in
``load_calculation.load_classes`` of its snapshot, and needs a new ``VERSION_ID``. The tests check that the model
``building_use`` runs the same SQL as before the classes. ``load_calculation.unclassified_load_class`` was added later
(``null`` unless the model is ``load_class``); a version without it kept the W/m² of the use (legacy value ``null``).

The MV stage takes the W/m² of the customers of a version of the model ``load_class`` from their LV load (installed
load over floor area) for the hall rule (:ref:`mv-customers`), also of a part without a class that took
``UNCLASSIFIED_LOAD_CLASS``, so both voltage levels give a customer the same W/m². The load editor of the HTTP API refuses versions of the model ``load_class``: it
computes the W/m² of the use.

Limitations
-----------

* The benchmarks are comparison values of the building stock for energy certificates, not measured loads of single
  buildings. They change in steps at their size limits.
* One value per class: universities (30-95 kWh/m² NGF) count as schools (10), acute hospitals (125) as health care in
  general (50). The InfDB classes do not separate them.
* The standard load profiles are meant for customers up to about 100 MWh per year; large customers have measured
  profiles. Production with process loads (machines, refrigeration) can exceed the benchmark values by far.
* A Commercial or Public part without a class keeps 79 or 29 W/m², or with ``UNCLASSIFIED_LOAD_CLASS`` the load of
  one class for all of them.
* The own peak divides the coincident load by the sim factor of the category (Commercial 0.5, Public 0.6), not of
  the class. No source gives coincidence factors of non-residential types across many buildings: Kerber (2011, p. 23)
  notes that no generally usable consumption data exist for commerce and industry, and the published factors per
  type (e.g. Siemens, Planung der elektrischen Energieverteilung, 2018, Tab. 2/3; Zander 2004 after Just 1983) relate
  the demand of one installation to its connected load.
* The 2015 benchmarks were replaced by the edition of 15 April 2021 (BAnz AT 03.05.2021 B1), which builds the
  comparison values from partial energy values per service (Teilenergiekennwerte) and the building's equipment. The
  2015 values per category are the last single values per use.

References
----------

* BMWi, BMUB (2015): Bekanntmachung der Regeln für Energieverbrauchswerte und der Vergleichswerte im
  Nichtwohngebäudebestand vom 7. April 2015. Bundesanzeiger, BAnz AT 21.05.2015 B3. An official announcement
  (amtliches Werk, § 5 UrhG).
* Meier, H., Fünfgeld, C., Adam, T., Schieferdecker, B. (1999): Repräsentative VDEW-Lastprofile. VDEW-Materialien
  M-28/99, Frankfurt am Main. Profile data: BDEW standard load profiles; typical days as in oemof demandlib
  (``src/oemof/demand/bdew/bdew_data/selp_series.csv``).
* Siemens AG (2018): Totally Integrated Power. Planning of Electric Power Distribution: Technical Principles,
  Tab. 2/3 "Average power demand of buildings according to their type of use".
* eGon-data: voltage levels of demand and generation by peak load (0.1 / 0.2 / 5.5 / 20 MW).
