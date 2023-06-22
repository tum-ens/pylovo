#-----------------------------CRUCIAL PYTHON TODOS-----------------------------#
# TODO: receive final pandapower net and verify correctness
# TODO: add interaction with urbs tool

import sys
import os

from flask import Flask, render_template, jsonify, request, session

#--------------------------------PURELY FOR DEBUG--------------------------------#
#uses a generic network to test visualization and editabilty in case the database is unavailable again
#can either use the pandapower-supplied default oberreihn-network or a custom one via json or csv-file

# import pandapower as pp
# import pandapower.networks as nw
# from pandapower.plotting.plotly.mapbox_plot import geo_data_to_latlong
# testnet = nw.mv_oberrhein()
# geo_data_to_latlong(testnet, projection='epsg:31467')

# testnet = pp.from_json('7068068.json')
# geo_data_to_latlong(testnet, projection='epsg:32632')

# net = pp.to_json(testnet)
#--------------------------------PURELY FOR DEBUG--------------------------------#

def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY='dev',
    )

    if test_config is None:
        # load the instance config, if it exists, when not testing
        app.config.from_pyfile('config.py', silent=True)
    else:
        # load the test config if passed in
        app.config.from_mapping(test_config)
    
    try:
        os.makedirs(app.instance_path)
    except OSError:
        pass

    from maptool.postcode_editor import bp as postcode_bp
    from maptool.network_editor import bp as network_bp
    from maptool.urbs_editor import bp as urbs_bp
    from maptool.urbs_results import bp as urbs_results_bp


    app.register_blueprint(postcode_bp)
    app.register_blueprint(network_bp)
    app.register_blueprint(urbs_bp)
    app.register_blueprint(urbs_results_bp)



    #On first opening, display postal code selection gui
    @app.route('/')
    def home():
        return render_template('postcode_editor/index.html')
        
    return app


    