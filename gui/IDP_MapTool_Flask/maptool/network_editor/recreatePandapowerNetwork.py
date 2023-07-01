import json
import pandapower as pp

def createFeatureFromGeoJSONProperties(featureProperties, featureListElement):
    new_feature_data = {}
    for property in featureProperties:
        value_to_insert = featureListElement.get(property)
        if value_to_insert == '' or value_to_insert == None:
            new_feature_data[property] = None
        elif not value_to_insert == None:
            if featureProperties[property]["type"] == 'float':
                new_feature_data[property] = float(value_to_insert)
            elif featureProperties[property]["type"] == 'int':
                new_feature_data[property] = int(value_to_insert)
            elif featureProperties[property]["type"] == 'boolean':
                new_feature_data[property] = bool(value_to_insert)
            else:
                new_feature_data[property] = value_to_insert

    return new_feature_data

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


def recreatePandapowerNetwork(net_features):
    newNet = pp.create_empty_network()

    line_std_typesList = net_features['line_stdList']

    for type in line_std_typesList:
        std_type_data = createFeatureFromGeoJSONProperties(line_std_properties, line_std_typesList[type])        
        pp.create_std_type(newNet, std_type_data, type)

    trafo_std_typesList = net_features['trafo_stdList']

    for type in trafo_std_typesList:
        std_type_data = createFeatureFromGeoJSONProperties(trafo_std_properties, trafo_std_typesList[type])        
        pp.create_std_type(newNet, std_type_data, type, element='trafo')

    trafo3w_std_typesList = net_features['trafo3w_stdList']

    for type in trafo3w_std_typesList:
        std_type_data = createFeatureFromGeoJSONProperties(trafo3w_std_properties, trafo3w_std_typesList[type])        
        pp.create_std_type(newNet, std_type_data, type, element='trafo3w')

    busList = net_features['busList']
    busListCoords = net_features['busListCoords']
    for idx in range(0, len(busList)):
        bus_data = createFeatureFromGeoJSONProperties(bus_properties, busList[idx])        

        pp.create_bus(newNet, index=bus_data["index"], name=bus_data["name"], vn_kv=bus_data["vn_kv"], geodata=busListCoords[idx], type=bus_data["type"], in_service=bus_data["in_service"], max_vm_pu=bus_data["max_vm_pu"],  min_vm_pu=bus_data["min_vm_pu"])

    lineList = net_features['lineList']
    lineListCoords = net_features['lineListCoords']

    for idx in range(0, len(lineList)):
        line_data = createFeatureFromGeoJSONProperties(line_properties, lineList[idx])  
        pp.create_line(newNet, name=line_data["name"], index=line_data["index"], from_bus=line_data["from_bus"], to_bus=line_data["to_bus"], length_km=line_data["length_km"], std_type=line_data["std_type"], geodata=lineListCoords[idx])

    trafoList = net_features['trafoList']
    for idx in range(0, len(trafoList)):
        trafo_data = createFeatureFromGeoJSONProperties(trafo_properties, trafoList[idx]) 
        pp.create_transformer(newNet, hv_bus=trafo_data['hv_bus'], lv_bus=trafo_data['lv_bus'], std_type=trafo_data['std_type'], name=trafo_data['name'], tap_pos=trafo_data['tap_pos'], in_service=trafo_data['in_service'], index=trafo_data['index'], max_loading_percent=trafo_data['max_loading_percent'], parallel=trafo_data['parallel'], df=trafo_data['df'], tap_dependent_impedance=trafo_data['tap_dependent_impedance'], vk_percent_characteristic=trafo_data['vk_percent_characteristic'], vkr_percent_characteristic=trafo_data['vkr_percent_characteristic'], pt_percent=trafo_data['pt_percent'], oltc=trafo_data['oltc'], xn_ohm=trafo_data['xn_ohm'])


    ext_gridList = net_features['ext_gridList']

    for idx in range(0, len(ext_gridList)):
        ext_grid_data = createFeatureFromGeoJSONProperties(ext_grid_properties, ext_gridList[idx]) 
        pp.create_ext_grid(newNet, bus=ext_grid_data['bus'], vm_pu=ext_grid_data['vm_pu'], va_degree=ext_grid_data['va_degree'], name=ext_grid_data['name'], in_service=ext_grid_data['in_service'], s_sc_max_mva=ext_grid_data['s_sc_max_mva'], s_sc_min_mva=ext_grid_data['s_sc_min_mva'], rx_max=ext_grid_data['rx_max'], rx_min=ext_grid_data['rx_min'], max_p_mw=ext_grid_data['max_p_mw'], min_p_mw=ext_grid_data['min_p_mw'], max_q_mvar=ext_grid_data['max_q_mvar'], min_q_mvar=ext_grid_data['min_q_mvar'], index=ext_grid_data['index'], r0x0_max=ext_grid_data['r0x0_max'], x0x_max=ext_grid_data['x0x_max'], controllable=ext_grid_data['controllable'], slack_weight=ext_grid_data['slack_weight'],)

    loadList = []
    sgenList = []
    switchList = []

    for bus in net_features['busList']:
        if not bus['load'] == {}:
            loadList.append(bus['load'])
        if not bus['sgen'] == {}:
            sgenList.append(bus['sgen'])
        if not bus['switch'] == {}:
            switchList.append(bus['switch'])
    

    for loads in loadList:
        # print(loads)
        for key in loads:
            load_data = createFeatureFromGeoJSONProperties(load_features, loads[key])  
            pp.create_load(newNet, bus=load_data['bus'], p_mw=load_data['p_mw'], q_mvar=load_data['q_mvar'], const_z_percent=load_data['const_z_percent'], const_i_percent=load_data['const_i_percent'], sn_mva=load_data['sn_mva'], name=load_data['name'], scaling=load_data['scaling'], in_service=load_data['in_service'], type=load_data['type'], max_p_mw=load_data['max_p_mw'], min_p_mw=load_data['min_p_mw'], max_q_mvar=load_data['max_q_mvar'], min_q_mvar=load_data['min_q_mvar'], controllable=load_data['controllable'])

    for sgens in sgenList:
        for key in sgens:
            sgen_data = createFeatureFromGeoJSONProperties(sgen_features, sgens[key])
            pp.create_sgen(newNet, bus=sgen_data['bus'], p_mw=sgen_data['p_mw'], q_mvar=sgen_data['q_mvar'], sn_mva=sgen_data['sn_mva'], name=sgen_data['name'], scaling=sgen_data['scaling'], type=sgen_data['type'], in_service=sgen_data['in_service'], max_p_mw=sgen_data['max_p_mw'], min_p_mw=sgen_data['min_p_mw'], max_q_mvar=sgen_data['max_q_mvar'], min_q_mvar=sgen_data['min_q_mvar'], controllable=sgen_data['controllable'], k=sgen_data['k'], rx=sgen_data['rx'], current_source=sgen_data['current_source'], generator_type=sgen_data['generator_type'], max_ik_ka=sgen_data['max_ik_ka'], kappa=sgen_data['kappa'], lrc_pu=sgen_data['lrc_pu'])

    for switches in switchList:
        for key in switches:
            switch_data = createFeatureFromGeoJSONProperties(switch_features, switch_features[key]) 
            pp.create_switch(newNet, bus=switch_data['bus'], element=switch_data['element'], et=switch_data['et'], closed=switch_data['closed'], type=switch_data['type'], name=switch_data['name'], z_ohm=switch_data['z_ohm'], in_ka=switch_data['in_ka'])

    return newNet