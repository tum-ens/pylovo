import json
import pandas as pd
import pandapower as pp
import os

from bs4 import BeautifulSoup
from maptool.urbs_results import bp
from flask import Flask, render_template, request, session
from maptool.network_editor.generateEditableNetwork import createGeoJSONofNetwork
from maptool.urbs_results.urbs_results_plotting import *
from maptool.config import *

#return main site html
@bp.route('/urbs_results', methods=['GET', 'POST'])
def urbs_results_setup():
    """
    called when the urbs results window is first opened
    finds the last changed directory in the urbs results directory, which we can assume to be the directory that has the results of our urbs run

    :return: path to the html file for the current window
    :rtype: string
    """
    urbs_result_dirs = [os.path.join(URBS_RESULT_PATH,d) for d in os.listdir(URBS_RESULT_PATH) if os.path.isdir(os.path.join(URBS_RESULT_PATH,d))]
    latest_subdir = max(urbs_result_dirs, key=os.path.getmtime)
    session['result_location'] = str(latest_subdir)

    return render_template('urbs_results/index.html')

@bp.route('/urbs_results/editableNetwork', methods=['GET', 'POST'])
def urbs_resultsNetwork():
    """
    on opening of the urbs results view the js code requests full information of the previously selected network
    we return the network with previously chosen and session-dependant plz, kcid and bcid with all features

    :return: a json filled with geoJSON featureCollections created from the network selected in previous steps
    :rtype: dict
    """
    if request.method == 'GET':
        #--------------------------------COMMENT OUT IF DATABASE CONNECTION DOES NOT WORK--------------------------------#
        testnet = pp.from_excel("pandapower2urbs\\dataset\\_transmission\\test.xlsx")
        #--------------------------------COMMENT OUT IF DATABASE CONNECTION DOES NOT WORK--------------------------------#

        #--------------------------------PURELY FOR DEBUG OR MISSING DATABASE CONNECTION--------------------------------#
        #from maptool import net as testnet
        #from .generateEditableNetwork import createFeatures
        #createFeatures(False, pp.from_json(testnet), 'bus',0,0,0)
        #return pp.to_json(testnet)
        #--------------------------------PURELY FOR DEBUG OR MISSING DATABASE CONNECTION--------------------------------#

        json_net = createGeoJSONofNetwork(testnet, True, True, True, True, True)
        json_net = json.dumps(json_net, default=str, indent=6)
        return json_net

#manages generation and returning of the html divs containing plotted data 
@bp.route('/urbs_results/plots', methods=['GET', 'POST'])
def urbs_results_generate_plot():
    """
    aggregate function for determining plotting behaviour. The names of the nodes of a given feature are extracted here before calling
    whatever plotting functions the user has defined in urbs_results_plotting.py. The html files returned by the plotting
    functions are turned into strings and sent to the frontend

    :return: a json containing all html plots for a feature as strings
    :rtype: json dict
    """
    if request.method == 'POST':
        print(os.getcwd())
        data_path = request.get_json()['type']
        feature_name_dict = request.get_json()['name']
        print(feature_name_dict)
        hdf_path = session.get('result_location') + '/' + URBS_RESULT_FILENAME

        #plot functions need to return the name of the generated plot
        #the returned name needs to be appended to the plot_filenames array
        
        plot_filenames = []
        if data_path == 'bus':
            feature_name = feature_name_dict['bus']
            #print(data_path, feature_name)
            plot_filenames.append(cap_pro_generate_plot(hdf_path=hdf_path, site_name=feature_name))
            plot_filenames.append(e_pro_in_generate_plot(hdf_path=hdf_path, site_name=feature_name))
            plot_filenames.append(e_pro_out_generate_plot(hdf_path=hdf_path, site_name=feature_name))
        if data_path == 'line':
            feature_name_from = feature_name_dict['from_bus']
            feature_name_to = feature_name_dict['to_bus']
            #print(data_path, feature_name_from, feature_name_to)
            #plot_filenames.append(cap_tra_generate_plot(hdf_path=hdf_path, site_name=feature_name_from))
            #plot_filenames.append(cap_tra_generate_plot(hdf_path=hdf_path, site_name=feature_name_to))
        if data_path == 'trafo':
            feature_name_lv = feature_name_dict['lv_bus']
            feature_name_hv = feature_name_dict['hv_bus']

            #insert plot function here

        if data_path == 'ext_grid':
            feature_name = feature_name_dict['bus']
            #insert plot function here


        return_json = {}
        for filename in plot_filenames:
            with open(URBS_RESULT_PLOT_SAVE_PATH + filename + '.html') as fp:
                soup = BeautifulSoup(fp, 'html.parser')
                return_json[filename] = str(soup)

        return return_json