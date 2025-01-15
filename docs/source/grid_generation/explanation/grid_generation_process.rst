Grid Generation Process
=======================

The functionalities of the grid generation process are divided into three classes:

.. autoclass:: pylovo.SyngridDatabaseConstructor.SyngridDatabaseConstructor

.. initializes database and read raw data.

.. autoclass:: pylovo.pgReaderWriter.PgReaderWriter

.. reads and writes from / to database.

.. autoclass:: pylovo.GridGenerator.GridGenerator

.. generates grids.

The higher level functions of the GridGenerator are explained in more detail since they contain the assumptions and logic
of grid generation. For a visual representation refer to :doc:`overview`.

Step 1
------

.. autofunction:: pylovo.GridGenerator.GridGenerator.cache_and_preprocess_static_objects

The selected zip code (PLZ) is searched in the table
:code:`postcode` and stored in the :code:`postcode_result table`. The zip code
defines the geographical area for which the network generation takes place.
The buildings, which are located in the area of the zip code, are selected from :code:`res` and :code:`oth`
and stored on :code:`buildings_tem`. The tables ending with :code:`tem`
are tables that temporarily store the data for grid generation. For
the buildings of the zip code the house distance is calculated and the settlement type is
is derived from it. The settlement type decides which transformer types are installed in the zip code.
Each building is assigned a maximum load.
This depends on the building type. For residential buildings, the load is
scaled to households, for other buildings (commercial, public,
industrial buildings), the building floor area is taken into account in the calculation of the power.
of the power. Buildings without load or with load over 100kW
are not part of the low voltage network and are therefore deleted.
Finally, the transformers from transformers are also transferred to :code:`buildings_tem`
are transferred.

Step 2
------

.. autofunction:: pylovo.GridGenerator.GridGenerator.preprocess_ways

The ways from :code:`ways`, which are located in the zip code area, will be
stored on :code:`ways_tem`. In way sections that overlap, connection nodes are created.
Then the buildings are connected to the ways.
For this purpose, a path section is created that leads perpendicularly
from the existing ways to the center of the building. Finally,
the buildings from :code:`buildings_tem` are assigned a node from :code:`ways_tem`.

Step 3
------

.. autofunction:: pylovo.GridGenerator.GridGenerator.apply_kmeans_clustering

Since the number of buildings in a postal code is too large for a coherent
network, the buildings are divided into subgroups.
The kmeans cluster algorithm divides the buildings into subgroups based on the geographic distance.
The number of kmeans clusters
for a postal code is usually single-digit. Each kmeans cluster is assigned an ID
(kcid, kmeans cluster ID).

Step 4
------

.. autofunction:: pylovo.GridGenerator.GridGenerator.position_substations

For the positioning of the transformers, existing transformers from OSM are considered first.
Buildings at a certain distance from the transformer are connected to the transformer.
The shape of the supply area transforms
from a circle (allowed linear distance), to a polygon,
because the distance to the consumers is measured along the streets.

Step 5
------

The kmeans clusters are further divided into so-called building clusters.
A buildings cluster becomes a network in which all buildings are connected to a low-voltage transformer.
The buildings are grouped
by means of an agglomerative hierarchical average linkage clustering.
The results of the hierarchical clustering can be displayed as a
Dendogram. The distance between two clusters is calculated as
average of all distances of buildings from cluster A to cluster B.
For this purpose, a distance matrix of the buildings is set up. After
each clustering step, there is a loop which verifies that the transformer power suffices to supply the consumers taking
into consideration
coincidence factor. The multilevel coincidence factor for each cable section is
evaluated by summing  the classified consumers (residential, public, commercial) of that cable section

Step 6
------

.. autofunction:: pylovo.GridGenerator.GridGenerator.install_cables

Branches are created to connect the consumers to the transformer, resulting in a radial network.
First, the consumers are connected to the road by cables.
A consumer is created as a :code:`consumer node` bus and every point
where several cables intersect as a :code:`connection node bus`. Next, the connections between the connection
nodes are drawn. Finally,
the connection nodes are connected to the transformers :code:`LVbus`.
The cables are run along the streets from :code:`ways_tem`. When the branches are created, the
Minimal Spanning Tree Algorithm determines a configuration of the network,
whose edge lengths are as short as possible and thus inexpensive. From a
repertoire of cable types, a suitable cable is selected. The process of
cable installation is based on the realistic decision making of a technician
and avoids the use of additional costly network components.

Step 7
------

.. autofunction:: pylovo.pgReaderWriter.PgReaderWriter.saveInformationAndResetTables

The data from the tables with the extension
:code:`tem` are deleted and transferred to the result tables :code:`bulidings_result`,
:code:`ways_result`.
