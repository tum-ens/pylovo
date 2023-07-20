from maptool.network_editor import bp

from syngrid.GridGenerator import GridGenerator

import pandapower as pp
import pandapower.networks as nw
from pandapower.plotting.plotly.mapbox_plot import geo_data_to_latlong

from flask import Flask, render_template, request, session

from pandapower2urbs import construct_model_components as pp2u

from maptool.network_editor.generateEditableNetwork import createGeoJSONofNetwork
from maptool.network_editor.recreatePandapowerNetwork import recreatePandapowerNetwork
import json

@bp.route('/networks', methods=['GET', 'POST'])
def networks():
    """
    once the Select Network button in the postcode view is pressed, we return the editable network view 
    
    :return: path to the network editor html file
    :rtype: string
    """
    return render_template('network_editor/index.html')

@bp.route('/networks/networkProperties', methods=['GET', 'POST'])
def networkProperties():
    """
    on networks window load the frontend fetches the network properties json file as well
    
    :return: json dict containing the properties of every network feature the user can edit
    :rtype: dict
    """
    f = open('maptool\\z_feature_jsons\\pandapower_network_features\\properties_final.json')
    data = json.load(f)
    return data

@bp.route('/networks/editableNetwork', methods=['GET', 'POST'])
def editableNetwork():
    """
    on networks window load the frontend fetches a network based on the plz id, kcid and bcid the user selected in the postcode editor.
    
    :return: json string of the pandapower network the user has requested
    :rtype: string
    """
    if request.method == 'GET':
        #plz id, kcid and bcid were previously cached in the postcode_editor/routes.py
        plz = session.get('plz')['value']
        kcid_bcid = session.get('kcid_bcid')['value']
        plz_version = session['plz_version']
        gg = GridGenerator(plz=plz, version_id=plz_version)
        pg = gg.pgr
        net_features = pg.read_net(plz=plz, kcid=kcid_bcid[0], bcid=kcid_bcid[1])
        print(net_features) #debug to compare pre- and post-editing network
        json_net = createGeoJSONofNetwork(net_features, True, True, True, True, True)
        json_net = json.dumps(json_net, default=str, indent=6)
        return json_net

    if request.method == 'POST':
        #print(request.get_json())
        return 'Success', 200

@bp.route('/networks/urbs_results', methods=['GET', 'POST'])
def urbs_results():
    """
    fetched when the "Finish Editing" button is clicked in the network editor
    
    :return: response indicating successful data transfer
    :rtype: JavaScript Fetch API Response
    """
    if request.method == 'POST':
        net = recreatePandapowerNetwork(request.get_json())
        #we save the trafo sn_mva in the session to create the kont trafo_data in the urbs setup step
        trafo_std_type = request.get_json()['trafoList'][0]['std_type']
        trafo_sn_mva = request.get_json()['trafo_stdList'][trafo_std_type]['sn_mva']
        session['trafo_sn_mva'] = trafo_sn_mva
        #we save the network to the right pdp2urbs directory for the urbs setup step
        pp.to_excel(net, "pandapower2urbs\\dataset\\_transmission\\test.xlsx")
        print(net) #debug to compare pre- and post-editing network
        return 'Success', 200