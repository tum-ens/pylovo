import pandas as pd
import json

f = open('maptool\\z_feature_jsons\\pandapower_network_features\\properties_final.json')
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

def extractPropertiesFromNet(input, index, properties):
    if input.empty:
        return {}
    output = {}
    input = input.fillna('')
    for idx in range(len(input.index)):
        output[idx] = {}
        for entry in input:
            output[idx][entry] = input.T.loc[entry].iloc[idx]
    return output

def createFeatures (isLines, ppdata, featureName, featureProperties, propertyGroupNames, propertyGroupFeatures):
    input_data = ppdata[featureName]
    input_data = input_data.fillna('')
    input_geoCoords = pd.DataFrame()

    if featureName == 'bus' or featureName == 'line':
        input_geoCoords = ppdata[featureName + '_geodata']
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

        if propertyGroupNames and propertyGroupFeatures:
            for property in propertyGroupNames:
                if property in ppdata:
                    try:
                        propertyGroup = ppdata[property].loc[ppdata[property].bus == index]
                    except KeyError:
                        propertyGroup = pd.DataFrame()
                extractedProperties = extractPropertiesFromNet(propertyGroup, index, propertyGroupFeatures[propertyGroupNames.index(property)])
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
    output = {}
    output['bus'] = createFeatures(False, net, 'bus', bus_properties, ['load', 'sgen', 'switch'], [load_features, sgen_features, switch_features]) if bus else {}
    output['trafo'] = createFeatures(True, net, 'trafo', trafo_properties, '', '') if trafo else {}
    output['line'] = createFeatures(True, net, 'line', line_properties, '', '') if line else {}
    output['ext_grid'] = createFeatures(False, net, 'ext_grid', ext_grid_properties, '', '') if ext_grid else {}
    output['std_types'] = extractStdTypes(net) if std_types else {}

    return output