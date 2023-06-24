# pylovopt (**py**thon **lo**w **vo**ltage **opt**imization tool)

>  Interdisciplinary Project at the Chair of Renewable and Sustainable Energy Systems at TUM (WS22-SS23)

A tool for displaying, editing and optimizing low-voltage distribution systems in real time
## Requirements
The main script runs in Python Flask (Version 3.8.16). Additional requirements are:
1. Pandapower: https://www.pandapower.org/
1. PostgreSQL: https://www.postgresql.org/
1. PostGIS: https://postgis.net/

## Installation

It is highly encouraged to set up a dedicated virtual environment for pylovopt. We recommend using the Python distribution Anaconda.

### Anaconda/Miniconda (recommended)
1. Anaconda (Python 3)/Miniconda. Choose the 64-bit installer if possible. During the installation procedure, keep both checkboxes "modify PATH" and "register Python" selected! If only higher Python versions are available, you can switch to a specific Python Version by typing conda install python=VERSION

## Getting Started
In a directory of your choice, clone this repository by executing:
```shell
git clone https://github.com/tum-ens/pylovopt.git --recurse-submodules
```

You want to use --recurse-submodules to pull the Connector-Syn-grid repo as well.

## Developing

### Setting up Dev

Open the Anaconda Powershell Prompt and navigate to the cloned directory folder and execute the following commands to set up the Anaconda environment:

```shell
cd /path/to/cloned/directory/pylovopt
conda env create -f environment.yml

cd ../Connector-syn-grid
pip install .

```


### Connecting to the Database
Open a terminal of your choice and enter the following command to connect to the database via localhost. Authenticate yourself with your own username and password. Without access to the database the tool will not work.
```shell
ssh -L 1111:localhost:5432 [user]@10.195.1.137
```

### Starting the Tool
Open the Anaconda Powershell Prompt and execute the following commands
```shell
conda activate TUM_Syngrid
cd /path/to/cloned/directory/pylovopt
flask --app maptool --debug run
```
Then navigate to http://127.0.0.1:5000 in a browser of your choice (though preferably Chrome, because that is the only browser the CSS layout is guaranteed to work with at the moment)

### Creating your own plots for the urbs result step
You can define your own plots in Python in maptool/urbs_results/urbs_results_plotting.py

A plot function needs to call plotly's to_html function with parameters full_html=False, include_plotlyjs=False and return the filename (without full path) as a string.
You can make sure the plot appears in the frontend by calling the new plotting function in the corresponding if clause in urbs_results_generate_plot() under maptool/urbs_results/routes.py. There are several example plots included that show the structure which needs to be followed


## Usage

Once you have opened the tool in a browser of your choice you will be presented with a map view centered somewhere in Bavaria.

![Alt text](fig/pylovopt_steps.jpg?raw=true "Title")

### Selecting an area for your networks
You can enter a German PLZ into the input in the top right corner to show all already existing nets for that PLZ. A popup window will let you select one of the available networks for that area, or you can choose to create a new version of the network.
Once you have submitted a valid PLZ, the tool will display the generated networks on the map. You can choose one of them by clicking on it or selecting it via the list on the right and clicking "Select Network"
1. You can select an area by drawing a shape on the map via the tools on the left side of the page and then clicking the "Select Area" button. At the moment this will only display all the buildings the database can find within the selected area. Clicking on individual buildings opens a popup that allows you to delete a specific building.
2. Once you are satisfied, you can click "Generate Network" to generate pylovo networks based on the buildings you selected. Once the networks have been generated, you can access each one of them by clicking on them
NOTE: at the moment there is a bug involving the building select, meaning that networks are based on the initial area selection; building deletion is not taken into consideration.

### Editing a selected network
3. Clicking the "Select Network" button will change your view, displaying the selected network in more detail. Now you can choose and edit individual features as well as delete them. <br>
You can access features via the lists on the right-hand side of the screen or by clicking on them on the map itself.

### Preparing the network for an urbs run
4. Once you are satisfied with your network, you can click the "Finish Editing" button and get taken to the Urbs Editor Setup view. Here you can once again access different features relevant for the urbs run through the GUI.
Once everything has been set to your liking you can click the "Finish Urbs Setup" button, which will start the execution of urbs.

## Style guide

## Useful links

The official Flask Documentation: https://flask.palletsprojects.com/en/2.2.x/quickstart/

W3Schools HTML Tutorials: https://www.w3schools.com/html/

W3Schools JavaScript Tutorials: https://www.w3schools.com/js/

## Licensing
