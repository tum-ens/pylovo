from maptool.postcode_editor import bp
from flask import request, session, Response
from syngrid.GridGenerator import GridGenerator
import pandapower as pp
from maptool.network_editor.generateEditableNetwork import createGeoJSONofNetwork
import json


@bp.route('/postcode', methods=['GET', 'POST'])
def postcode():
    """
    When user submits postal code or area selection in gui we return the corresponding postal code area boundary
    """
    if request.method == 'POST':
        plz = {'value' : request.get_json()}
        session['plz'] = plz
        gg = GridGenerator(plz=request.get_json())
        pg = gg.pgr
        versions = pg.getAllVersionsofPLZ(request.get_json())

        return versions

#fetched from the postcode version select gui, caches the selected network version
@bp.route('/postcode/plz/version', methods=['GET', 'POST'])
def postcodePlzVersion():
    if request.method == 'POST':
        plz_version = request.get_json()
        session['plz_version'] = plz_version
        
        gg = GridGenerator(plz=session['plz']['value'], version_id=plz_version)
        
        #if a network already exists for the selected version, the code stops, otherwise a new grid is generated
        #either way, we return sucess unless we catch an error during grid generation
        try:      
            gg.generate_grid()
        except ValueError as ve:
            print(ve)
            return 'Failure', -100
        
        return 'Success', 200

#called if the user uses an id to define network area, returns the area's boundary shape
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
        return postcode_boundary

@bp.route('/postcode/nets', methods=['GET', 'POST'])
def postcodeNets():
    #fetched once the user has selected one of the preview nets in a plz code area. The JS code returns the kcid and bcid of the chosen network
    if request.method == 'POST':
        kcid_bcid = {'value' : request.get_json()}
        session['kcid_bcid'] = kcid_bcid
        return 'Success', 200
    
    #fetched after the frontend has displayed the network area boundary, returns all networks contained in the area
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

#called if the user uses a shape to define a network area, fetches all buildings within the area and returns them to the frontend
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

#called once the user has finished deleting buildings in the frontend
#TODO: building selection based on building osm_ids returned from frontend
@bp.route('/postcode/area/new-net-id', methods=['GET', 'POST'])
def postcodeAreaNewId():
    if request.method =='POST':
        print(request.get_json())
        plz = request.get_json()['ID']
        version = request.get_json()['version']
        buildings = request.get_json()['buildings']

        res = []
        oth = []

        for building in buildings:
            if building['type'] == 'res':
                res.append(building['id'])
            elif building['type'] == 'oth':
                oth.append(building['id'])

        print(res, oth)

        shape = session["new_area_shape"]
        gg = GridGenerator(plz=str(plz), geom_shape=shape, version_id=str(version))
        try:
            gg.generate_grid_from_geom(buildings={'res': res, 'oth': oth})
        except ValueError as ve:
            print(ve)
            return Response(status=400)
        return 'Success', 200

