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

#once the Select Network button is pressed, we return the editable network view 
@bp.route('/networks', methods=['GET', 'POST'])
def networks():
    return render_template('network_editor/index.html')

@bp.route('/networks/networkProperties', methods=['GET', 'POST'])
def networkProperties():
    f = open('maptool\\z_feature_jsons\\pandapower_network_features\\properties_final.json')
    data = json.load(f)
    return data

@bp.route('/networks/editableNetwork', methods=['GET', 'POST'])
def editableNetwork():
    #on opening of the network view the js code requests full information of the previously selected network
    if request.method == 'GET':
        plz = session.get('plz')['value']
        kcid_bcid = session.get('kcid_bcid')['value']
        plz_version = session['plz_version']
        gg = GridGenerator(plz=plz, version_id=plz_version)
        pg = gg.pgr
        net_features = pg.read_net(plz=plz, kcid=kcid_bcid[0], bcid=kcid_bcid[1])
        #pp.to_excel(net_features, "example1.xlsx")
        print(net_features)
        json_net = createGeoJSONofNetwork(net_features, True, True, True, True, True)
        json_net = json.dumps(json_net, default=str, indent=6)
        return json_net

    if request.method == 'POST':
        #print(request.get_json())
        return 'Success', 200
    
@bp.route('/networks/urbs_results', methods=['GET', 'POST'])
def urbs_results():
    if request.method == 'POST':
        net = recreatePandapowerNetwork(request.get_json())
        trafo_std_type = request.get_json()['trafoList'][0]['std_type']
        trafo_sn_mva = request.get_json()['trafo_stdList'][trafo_std_type]['sn_mva']
        pp.to_excel(net, "pandapower2urbs\\dataset\\_transmission\\test.xlsx")
        session['trafo_sn_mva'] = trafo_sn_mva

        #pp.to_excel(net, "example2.xlsx")
        print(net)
        return 'Success', 200