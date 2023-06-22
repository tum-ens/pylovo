from maptool.postcode_editor import bp
from flask import request, session, Response
from syngrid.GridGenerator import GridGenerator
import pandapower as pp
from maptool.network_editor.generateEditableNetwork import createGeoJSONofNetwork
import json

#When user submits postal code or area selection in gui we return the corresponding postal code area boundary
@bp.route('/postcode', methods=['GET', 'POST'])
def postcode():
    if request.method == 'POST':
        plz = {'value' : request.get_json()}
        session['plz'] = plz
        gg = GridGenerator(plz=request.get_json())
        pg = gg.pgr
        versions = pg.getAllVersionsofPLZ(request.get_json())

        return versions

@bp.route('/postcode/plz/version', methods=['GET', 'POST'])
def postcodePlzVersion():
    if request.method == 'POST':
        plz_version = request.get_json()
        session['plz_version'] = plz_version
        
        gg = GridGenerator(plz=session['plz']['value'], version_id=plz_version)
        try:
            gg.generate_grid()
        except ValueError as ve:
            print(ve)
            return 'Failure', -100
        
        return 'Success', 200

@bp.route('/postcode/plz', methods=['GET', 'POST'])
def postcodePlz():
    if request.method == 'POST':
        plz = {'value' : request.get_json()}
        session['plz'] = plz
        gg = GridGenerator(plz=request.get_json())
        pg = gg.pgr
        version = session['plz_version']
        postcode_gdf = pg.getGeoDataFrame(table="postcode_result", id=request.get_json(), version_id=version)
        postcode_boundary = postcode_gdf.to_crs(epsg=4326).boundary.to_json()
        print(postcode_boundary)
        return postcode_boundary

#called if the user decides to use a shape to define a network area
@bp.route('/postcode/area', methods=['GET', 'POST'])
def postcodeArea():
    if request.method == 'POST':
        shape = str(request.get_json()['features'][0]['geometry'])
        session["new_area_shape"] = shape
        gg = GridGenerator(plz='199999', geom_shape=shape)
        res_buildings = gg.pgr.test__getBuildingGeoJSONFromShapefile('res', shape)
        oth_buildings = gg.pgr.test__getBuildingGeoJSONFromShapefile('oth', shape)
        #gg.generate_grid_from_geom()
        return {"res_buildings" : res_buildings, "oth_buildings" : oth_buildings}


@bp.route('/postcode/area/new-net-id', methods=['GET', 'POST'])
def postcodeAreaNewId():
    if request.method =='POST':
        print(request.get_json())
        plz = request.get_json()['ID']
        version = request.get_json()['version']
        shape = session["new_area_shape"]
        gg = GridGenerator(plz=str(plz), geom_shape=shape, version_id=str(version))
        try:
            gg.generate_grid_from_geom()
        except ValueError as ve:
            print(ve)
            return Response(status=400)
        return 'Success', 200

@bp.route('/postcode/area/buildings', methods=['GET', 'POST'])
def postcodeAreaBuildings():
    if request.method =='POST':
        print("method")
        return 'Success', 200

#called once the user has selected one of the preview nets in a plz code area. The JS code returns the kcid and bcid of the network
@bp.route('/postcode/nets', methods=['GET', 'POST'])
def postcodeNets():
    if request.method == 'POST':
        kcid_bcid = {'value' : request.get_json()}
        session['kcid_bcid'] = kcid_bcid
        return 'Success', 200
    
    #After receiving the postal code boundary the js code requests all nets of the selected version and plz
    if request.method == 'GET':
        plz = session.get('plz')['value']
        gg = GridGenerator(plz=plz)
        pg = gg.pgr
        nets = pg.getAllNetsOfVersion(plz, session['plz_version'])
        netList = []

        for kcid, bcid, grid in nets:
            grid_json_string = json.dumps(grid)
            net = pp.from_json_string(grid_json_string)
            net_json = json.dumps(createGeoJSONofNetwork(net, False, False, True, False, False), default=str, indent=6)
            netList.append([kcid, bcid, net_json])
            print("done ", kcid, bcid)

        print("returning")
        return netList
