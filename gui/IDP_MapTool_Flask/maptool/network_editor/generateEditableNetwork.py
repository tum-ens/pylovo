import pandas as pd
import json
import os

#input (dict): contains all secondary features of a type for a single primary feature (e.g. all loads of a bus)
def extractPropertiesFromNet(input):
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

#creates and returns a geoJSON object for a feature of the original pandapower network
#isLines                (bool): flag for if the geojson object to create is going to be a line or point object
#ppdata                 (dict): the pandapower network object
#featureName            (string): determines how we handle feature extraction and geojson creation
#featureProperties      (list[string]): contains the names of all properties for a feature (e.g. bus, line etc)
#propertyGroupNames     (list[string]): only relevant for bus features; contains names of all secondary features we want to add (switch, sgen, load)
#propertyGroupFeatures  (list[list[string]]): only relevant for bus features; contains the names of all properties for a secondary feature 
def createFeatures (isLines, ppdata, featureName, featureProperties, propertyGroupNames, propertyGroupFeatures):
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
    return json.dumps(ppdata.std_types)


def createGeoJSONofNetwork(net, bus, trafo, line, ext_grid, std_types):
    documentation_path =  os.path.abspath('../gui/IDP_Maptool_Flask/maptool\\z_feature_jsons\\pandapower_network_features\\properties_final.json')
    directory_path = 'maptool\\z_feature_jsons\\pandapower_network_features\\properties_final.json'
    f = open(directory_path)
    data = json.load(f)

    line_std_properties = data['std_type']['line']
    trafo_std_properties = data['std_type']['trafo']
    trafo3w_std_properties = data['std_type']['trafo3w']

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