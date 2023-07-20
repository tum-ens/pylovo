from maptool.postcode_editor import bp
from flask import request, session, Response
from syngrid.GridGenerator import GridGenerator
import pandapower as pp
from maptool.network_editor.generateEditableNetwork import createGeoJSONofNetwork
import json


@bp.route('/postcode', methods=['GET', 'POST'])
def postcode():
    """
    When user submits postal code in gui we return all versions of the id for which networks are found in the database

    :return: list of all versions
    :rtype: list[string]
    """
    if request.method == 'POST':
        plz = {'value' : request.get_json()}
        session['plz'] = plz
        gg = GridGenerator(plz=request.get_json())
        pg = gg.pgr
        versions = pg.getAllVersionsofPLZ(request.get_json())

        return versions

@bp.route('/postcode/plz/version', methods=['GET', 'POST'])
def postcodePlzVersion():
    """
    fetched from the postcode version select gui, caches the selected network version and tries to generate a network grid for it
    
    :return: response indicating whether a network grid of the requested version already exists
    :rtype: JavaScript Fetch API Response
    """
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
def postcodeReturnAreaBoundary():
    """
    called if the user uses an id to define network area, returns the area's boundary shape for display on the GUI map

    :return: a geoJSON feature dict containing the shape information of the area boundary 
    :rtype: dict
    """
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
def postcodeFetchNetsForID():
    """
    the JS Fetch API sends a POST request once the user has selected one of the preview nets 
    The request includes the kcid and bcid of the chosen network

    :return: response indicating successful data transfer
    :rtype: JavaScript Fetch API Response

    the JS Fetch API sends a GET request immediately after receiving and displaying the boundary area shape of a PLZ region

    :return: a list geojson featurecollections of all networks associated with the id the user selected
    :rtype: list
    """

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
            #we only display the lines of all preview nets for performance reasons
            net_json = json.dumps(createGeoJSONofNetwork(net, False, False, True, False, False), default=str, indent=6)
            netList.append([kcid, bcid, net_json])
            print("done ", kcid, bcid)

        print("returning")
        return netList

@bp.route('/postcode/area', methods=['GET', 'POST'])
def postcodeAreaReturnBuildings():
    """
    called if the user uses a shape to define a new network area, fetches all buildings within the area and returns them to the frontend

    :return: dict containing geoJSON feature objects for residential buildings and other buildings. The geoJSON objects also hold each building's osmID
    :rtype: dict
    """
    if request.method == 'POST':
        shape = str(request.get_json()['features'][0]['geometry'])
        session["new_area_shape"] = shape
        gg = GridGenerator(plz='199999', geom_shape=shape)
        res_buildings = gg.pgr.test__getBuildingGeoJSONFromShapefile('res', shape)
        oth_buildings = gg.pgr.test__getBuildingGeoJSONFromShapefile('oth', shape)
        #gg.generate_grid_from_geom()
        return {"res_buildings" : res_buildings, "oth_buildings" : oth_buildings}

@bp.route('/postcode/area/new-net-id', methods=['GET', 'POST'])
def postcodeAreaCreateNewGridFromBuildings():
    """
    called once the user has finished deleting buildings in the frontend. It extracts the osmID of each building
    sent from the frontend, separates them into residential and other buildings and calls the pylovo functions necessary for generating a new grid

    :return: response indicating whether a new network grid was sucessfully created
    :rtype: JavaScript Fetch API Response
    """
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


        shape = session["new_area_shape"]
        gg = GridGenerator(plz=str(plz), geom_shape=shape, version_id=str(version))
        try:
            gg.generate_grid_from_geom(buildings={'res': res, 'oth': oth})
        except ValueError as ve:
            print(ve)
            return Response(status=400)
        return 'Success', 200

