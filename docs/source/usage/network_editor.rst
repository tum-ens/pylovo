Network Editor
***************
In the network editor view the user can fine-tune individual features and extend the network by adding or removing new features, before handing over the network for
setup of the urbs run.

Editing features
=================
| The user can select a feature either by clicking on it on the map or by selecting the feature type from the button column on the left side of the screen and then choosing
  the feature they want to edit from the newly appeared list. 

Editing std_type features
-------------------------
| Std_types are assigned to lines and trafos. Since they have to be consistent for all lines/trafos their properties can only be adjusted in the std_type editor accessible
  over the left-side button column. Std_type properties are still listed in the line & trafo editors, but cannot be changed. Instead you select which std_type you want to 
  assign to each trafo & line individually.

Adding features
===============

| Every feature type list has an "Add [Feature]" button at the top. Clicking this button changes the map settings to draw mode. You can exit draw mode at any time
  by pressing **esc**

Adding busses
-------------
| Busses can be added anywhere on the map without restrictions. Simply click on the map where you want the bus to appear. The corresponding editor window will
  automatically be opened.
| IMPORTANT NOTE: The tool makes no assumptions about where a bus is placed. It is up to the user to ensure the placement makes logical sense in the context of the 
  current network.

Adding lines
------------
| Lines need to connect to two different busses to be viable. The user needs to start drawing by clicking on a bus, otherwise the tool will immediately exit draw mode.
  After starting from a bus, the user can draw as many intermediate points as they want. To finish creating the line the user needs to double click on a bus again.
| IMPORTANT  NOTE: Using urbs requires there be no circular line connections in the network. At the moment the tool does not check that this is the case. It is up to the user
  to make sure newly created lines do not create circles in the network graph.

Adding trafos
-------------
| Adding a trafo works the same way as adding a line. However, the network only allows for one trafo at a time. As long as a trafo exists, 
  the "Add Trafo" button will be unavailable.


Adding external grids
---------------------
| Adding external grids works much in the same way as adding busses, with the difference, that external grids need to be placed on an already existing bus.
  Once the user has entered draw mode, they can create a new grid by double clicking on a bus on the map.
Adding secondary features
=========================

Deleting features
=================