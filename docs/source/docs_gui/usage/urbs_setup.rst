Urbs Setup Editor
******************

| This editor is used for setting up all parameters we need for the eventual run of our optimization model
  via urbs.

Features
========

Building
--------
| Here parameters for individual buildings are set. Each building corresponds to a bus with load from previous steps. The data entered here is supplemented with additional information
  fetched from the database.

Demand
--------
| Demands are time series for an entire year measured in hours describing how much of a commodity is requested at any given hour. At the moment demand profiles are predefined for
  a select few commodities. For these commodities the user can select as many profiles as they want.

Transmission
----------------
| Describes possible transports of commodities between buses. The default trafo profile "kont" is generated based on the "sn_mva" of the trafo set in the network editor step.

Global
--------
| Here we set a few global values needed for the urbs run.

Commodity
---------
| Commodities are goods that can be generated, stored, transmitted or consumed. They come in several types: 

* **Buy:** commodities which can be sold with a buy price that may vary for each time step
* **Sell:** commodities which can be sold with a sell price that may vary for each time step
* **SupIm:** stands for Supply Intermittent commodities for which availability is not constant
* **Stock:** commodities which may be purchased for a fixed price
* **Demand:** requested commodities of the energy system, usually end products of the model

| Since demand and SupIm rely on predefined profiles at the moment, we recommend the user not to set them as types.
| Commodities can and must be attached to processes to be considered in the urbs model


Process
--------
| Processes change input commodities into output commodities. Each process can have multiple commodities attached to itself, regardless of any other process.
  

Process-Commodity
^^^^^^^^^^^^^^^^^^
| Process-Commodities are secondary features attached to individual processes. They are defined as input or output commodities.

Process-Configuration
------------------------
| A table that defines how many of each process are attached to each bus with loads. We distinguish between 3 value types:

* **Empty field:** It is forbidden to add any amount of this process to this bus
* **field value = 0:** There are currently none of this process attached to this bus, but it is possible for the optimization model to add to it during runtime
* **field value \> 0:** Sets an upper bound for how many of this process can be attached to this bus 

Storage
------------------------
| Describes multiple technical facilities that can store energy. Each is connected to one commodity. 

Storage-Configuration
------------------------
| A table that defines how many of each storage type are attached to each bus with loads. We distinguish between 3 input types:

* **Empty field:** It is forbidden to add any amount of this storage to this bus
* **field value = 0:** There are currently none of this storage attached to this bus, but it is possible for the optimization model to add to it during runtime
* **field value \> 0:** Sets an upper bound for how many of this storage can be attached to this bus 

SupIm
--------
| SupIm (Supply Intermittent) are time series for an entire year measured in hours describing how much of a commodity of type SupIm is requested at any given hour. At the moment demand profiles are predefined for
  a select few commodities. For these commodities the user can select as many profiles as they want.


Adding commodities
==================
| You can add new commodities by selecting the commodity (C) tab on right side of the screen and pressing the "Add Commodity" button.
| A popup window will open. Enter the name of the new commodity and click the "Add Commodity" button to finish the process.

Adding Buy-Sell-Price 
---------------------
This feature is work in progress

Adding processes
================
| You can add new commodities by selecting the process (P) tab on right side of the screen and pressing the "Add Process" button.
| A popup window will open. Adding a process requires attaching at least one commodity to it. You can select one of the pre-existing commodities via the dropdown menu.
| You can also choose to select "New Commodity" from the dropdown. After setting the name and clicking "Create Process" the process and the new commodity will be added to their
  respective lists.

Adding process commodities
---------------------------
| You have to add one commodity on creation of a given process. Additional process commodities can be added by selecting a process in the GUI and scrolling down to the 
  bottom of the editor window. Clicking on the "Add PRO_COM_PROP" button will allow you to add a preexisting or new commodity and set it as input or output commodity.