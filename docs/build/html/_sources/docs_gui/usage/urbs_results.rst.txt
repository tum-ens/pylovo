Urbs Results
***************

| This window visualizes the results of the urbs run through predefined plots. In the backend data is extracted from the urbs hdf5 output file and 
  converted into a plot, which is converted to html and sent to the frontend.

Showing outputs
===============
| You can show the output for any given feature by either selecting it on the map or via the tabs on the right side of the screen

Defining outputs
================
| All plots are defined in the file **urbs_results_plotting.py** and called in the function *urbs_results_generate_plot* of the corresponding **routes.py** file.
| The user can define additional plots and call them by adding them to the **routes.py** file. Every plot function needs to fulfill certain criteria to work properly.
| Each function needs to take the following inputs:

* **hdf_path**: path to the hdf5 save location, generated automatically by the **routes.py** code, just needs to be added as a variable to the function call
* **site_name**: for buses we need a single site name, for lines both site names as keys to query the hdf5 file for data. The names are returned from the frontend by default and simply need to be added to the function calls as variables
* **save_path**: path to the plot save location. It is automatically set in the function definition and should not be changed by the user

| Furthermore the function needs to return the filename the user has defined within the plotting function.
| Last but not least the user needs to call the plotly *write_html* function with the following parameters:

* **file= save_path + filename + ".html"**: Sets the absolute save path of the plot
* **full_html = false:**: makes sure we generate a single div to be inserted into our existing webpage instead of generating a full independent webpage
* **include_plotlyjs=False**: we don't need to insert the plotlyjs code into our html because we are adding it via CDN in our base html code
* **div_id=site_name + filename**: we need to give the html div generated from the plot a unique ID for later insertion in the frontend

