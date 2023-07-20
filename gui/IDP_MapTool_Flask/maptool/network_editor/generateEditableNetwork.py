import pandas as pd
import json
import os

def extractPropertiesFromNet(input):
    """
    helper function creating a properties dict for secondary features (e.g. loads of a bus) to be included in the properties
    of the final feature geoJSON

    :param input: contains all secondary features of a type for a single primary feature
    :type input: dict
    :return: json containing key value pairs of secondary feature properties and their values for every primary feature
    :rtype: dict
    """
    if input.empty:
        return {}
    output = {}
    input = input.fillna('')
    #a feature may have multiple secondary features of a type
    #we add all of them with simple int counters as indices
    for idx in range(len(input.index)):
        output[idx] = {}
        for entry in input:
            output[idx][entry] = input.T.loc[entry].iloc[idx]
    return output


def createFeatures (isLines, ppdata, featureName, featureProperties, propertyGroupNames, propertyGroupFeatures):
    """
    creates and returns a geoJSON object for a feature of the original pandapower network
    
    :param isLines: flag for if the geojson object to create is going to be a line or point object
    :type isLines: bool
    :param ppdata: the pandapower network object
    :type ppdata: dict
    :param featureName: determines how we handle feature extraction and geojson creation
    :type featureName: string
    :param featureProperties: contains the names of all properties for a feature (e.g. bus, line etc)
    :type featureProperties: list[string]
    :param propertyGroupNames: only relevant for bus features; contains names of all secondary features we want to add (switch, sgen, load)
    :type propertyGroupNames: list[string]
    :param propertyGroupFeatures: only relevant for bus features; contains the names of all properties for a secondary feature
    :type propertyGroupFeatures: list[list[string]]
    :return: geoJSON dict containing all features of a single type (bus, line etc) of a network
    :rtype: dict 
    """
    input_data = ppdata[featureName]
    input_data = input_data.fillna('')
    input_geoCoords = pd.DataFrame()
    
    #only busses and lines have their own coordinate data, ext_grids and trafo geojsons get coords based on the busses they're attached to
    if featureName == 'bus' or featureName == 'line':
        input_geoCoords = ppdata[featureName + '_geodata']
        #the bus coords in the pandapower network by default have a column we don't need, so we drop it
        if featureName == 'bus':
            input_geoCoords = input_geoCoords.drop(['coords'], axis=1)
    else:
        input_geoCoords = ppdata.bus_geodata
        temp = pd.DataFrame()

        if featureName == 'ext_grid' :
            for bus in input_data['bus']:
                ext_geocoords = input_geoCoords.T[bus].to_frame().T
                temp = pd.concat([temp, ext_geocoords], ignore_index=True)
            temp = temp.drop(['coords'], axis=1)

        elif featureName == 'trafo':
            i = 0
            for hv_bus, lv_bus in zip(input_data['hv_bus'], input_data['lv_bus']):
                hv_geocoords = input_geoCoords.T[hv_bus]
                lv_geocoords = input_geoCoords.T[lv_bus]
                tempLine = pd.Series(data={'hv_bus' : [hv_geocoords.x, hv_geocoords.y], 'lv_bus' : [lv_geocoords.x, lv_geocoords.y]}).to_frame().T
                temp = pd.concat([temp, tempLine], ignore_index=True)
        input_geoCoords = temp

    input_geoJSON = {"type" : "FeatureCollection", "features": []}

    #each set of coordinates defines a feature to be turned into a geojson
    for point in input_geoCoords.T:
        currentFeatureProperties = {}
        index = 0
        if featureProperties:
            index = point if (featureName == 'bus' or featureName == 'line') else input_data.index[point]
            currentFeatureProperties["index"] = index
            
            for property in featureProperties:
                if not property == 'index':
                    if property in input_data:
                        currentFeatureProperties[property] = input_data.loc[index][property]
                    else:
                        currentFeatureProperties[property] = ""
        #if a feature has secondary features (e.g bus and load) they are extracted here and added to the geojson properties
        if propertyGroupNames and propertyGroupFeatures:
            for property in propertyGroupNames:
                if property in ppdata:
                    try:
                        propertyGroup = ppdata[property].loc[ppdata[property].bus == index]
                    except KeyError:
                        #we want to include the secondary feature in our geojson even if it doesn't exist in the original net, 
                        #since the user might want to add it in the gui
                        propertyGroup = pd.DataFrame()
                extractedProperties = extractPropertiesFromNet(propertyGroup)
                currentFeatureProperties[property] = extractedProperties
        
        inputCoordinates = input_geoCoords.loc[point].tolist() 
        if featureName == 'line':
            inputCoordinates = inputCoordinates[0]   
        feature = { "type": "Feature", 
                        "geometry": {"type": "LineString" if isLines else "Point", 
                                     "coordinates": inputCoordinates}, 
                        "properties": currentFeatureProperties
                    }
        input_geoJSON["features"].append(feature)
    return input_geoJSON

def extractStdTypes(ppdata):
    """
    std_types are already saved in a convenient datastructure in the pandapower network, so we just extract them as is

    :return: json string of all std_types grouped by feature (line, trafo)
    :rtype: string
    """
    return json.dumps(ppdata.std_types)


def createGeoJSONofNetwork(net, bus, trafo, line, ext_grid, std_types):
    """
    creates the final json object that contains geoJSON objects for every feature type we want to display on the GUI map
    
    :param net: the pandapower network
    :type net: dict
    :param bus: flag determining whether we want to include busses in the displayable network
    :type bus: bool
    :param trafo: flag determining whether we want to include trafos in the displayable network
    :type trafo: bool
    :param line: flag determining whether we want to include lines in the displayable network
    :type line: bool
    :param ext_grid: flag determining whether we want to include ext_grids in the displayable network
    :type ext_grid: bool
    :param std_types: flag determining whether we want to include std_types in the frontend editor
    :type std_types: bool
    :return: json object containing geojsons for every feature of the original pdp net we want to display
    :rtype: dict
    """
    directory_path = 'maptool\\z_feature_jsons\\pandapower_network_features\\properties_final.json'
    f = open(directory_path)
    data = json.load(f)

    line_properties = data['line']

    ext_grid_properties = data['ext_grid']

    bus_properties = data['bus']
    load_features = data['load']
    sgen_features = data['sgen']
    switch_features = data['switch']

    trafo_properties = data['trafo']
    trafo3w_properties = data['trafo3w']
    f.close()

    output = {}
    output['bus'] = createFeatures(False, net, 'bus', bus_properties, ['load', 'sgen', 'switch'], [load_features, sgen_features, switch_features]) if bus else {}
    output['trafo'] = createFeatures(True, net, 'trafo', trafo_properties, '', '') if trafo else {}
    output['line'] = createFeatures(True, net, 'line', line_properties, '', '') if line else {}
    output['ext_grid'] = createFeatures(False, net, 'ext_grid', ext_grid_properties, '', '') if ext_grid else {}
    output['std_types'] = extractStdTypes(net) if std_types else {}

    return output