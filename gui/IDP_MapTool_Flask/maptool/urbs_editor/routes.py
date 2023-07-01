from maptool.urbs_editor import bp
from flask import Flask, render_template, request, session
from syngrid.GridGenerator import GridGenerator
import pandapower as pp
import pandas as pd
import numpy as np
import os
from maptool.network_editor.generateEditableNetwork import createGeoJSONofNetwork
import json
from pandapower2urbs import construct_model_components as pp2u
from maptool.config import *


#GUROBI LINE COMMANDS:
#prop=read('filename')
#prob.computeIIS()
#prop.write('infeasible.ilp')


#As a frist step we always return the html for the current window
@bp.route('/urbs', methods=['GET', 'POST'])
def urbs_setup():
    return render_template('urbs_editor/index.html')

#one page load javascript fetches the json file containing parameter names, types, default values etc for generation and maintenance of the editor environment
@bp.route('/urbs/urbs_setup_properties', methods=['GET', 'POST'])
def urbs_setup_properties():
    if request.method == 'GET':
        f = open('maptool\\z_feature_jsons\\urbs_setup_features\\urbs_setup_features.json', 'r')
        urbs_setup_props = json.load(f)
        return urbs_setup_props


@bp.route('/urbs/editableNetwork', methods=['GET', 'POST'])
def editableNetwork():
    #on opening of the network view the js code requests full information of the previously selected network
    #we return the network with previously chosen and session-dependant plz, kcid and bcid with all features
    if request.method == 'GET':
        #--------------------------------COMMENT OUT IF DATABASE CONNECTION DOES NOT WORK--------------------------------#
        # plz = session.get('plz')['value']
        # kcid_bcid = session.get('kcid_bcid')['value']
        # plz_version = session['plz_version']
        # gg = GridGenerator(plz=plz, version_id=plz_version)
        # pg = gg.pgr
        testnet = pp.from_excel("pandapower2urbs\\dataset\\_transmission\\test.xlsx")
        #--------------------------------COMMENT OUT IF DATABASE CONNECTION DOES NOT WORK--------------------------------#

        #--------------------------------PURELY FOR DEBUG--------------------------------#
        #from maptool import net as testnet
        #from .generateEditableNetwork import createFeatures
        #createFeatures(False, pp.from_json(testnet), 'bus',0,0,0)
        #return pp.to_json(testnet)
        #--------------------------------PURELY FOR DEBUG--------------------------------#

        json_net = createGeoJSONofNetwork(testnet, True, True, True, True, True)
        json_net = json.dumps(json_net, default=str, indent=6)
        return json_net

    #does nothing atm
    if request.method == 'POST':
        #print(request.get_json())
        return 'Success', 200

#at them moment we use purely pre-defined profiles the user can choose from. They are extracted from the csv template and sent to the frontend here   
@bp.route('/urbs/demand_profiles', methods=['GET', 'POST'])
def demandProfiles():
    demand_electricity = pd.read_csv(os.path.join(os.getcwd(), 'pandapower2urbs/dataset/demand/profiles/electricity.csv'), sep=',')
    demand_electricity_reactive = pd.read_csv(os.path.join(os.getcwd(),'pandapower2urbs/dataset/demand/profiles/electricity-reactive.csv'), sep=',')
    demand_mobility = pd.read_csv(os.path.join(os.getcwd(),'pandapower2urbs/dataset/demand/profiles/mobility.csv'), sep=',')
    demand_space_heat = pd.read_csv(os.path.join(os.getcwd(),'pandapower2urbs/dataset/demand/profiles/space_heat.csv'), sep=',')
    demand_water_heat = pd.read_csv(os.path.join(os.getcwd(),'pandapower2urbs/dataset/demand/profiles/water_heat.csv'), sep=',')

    demand_json = {"demand_electricity" : demand_electricity.to_json(),
            "demand_electricity_reactive" : demand_electricity_reactive.to_json(),
            "demand_mobility" : demand_mobility.to_json(), 
            "demand_space_heat" : demand_space_heat.to_json(),
            "demand_water_heat" : demand_water_heat.to_json()
            }

    return demand_json
    
@bp.route('/urbs/transmission_profiles', methods=['GET', 'POST'])
def formatTransmissionSetup():
    trafo_data = pd.read_csv(os.path.join(os.getcwd(), 'pandapower2urbs_dataset_template/dataset/_transmission/trafo_data.csv'), sep=',')
    trans_json = {
        "trafo_data" : trafo_data.to_json(),
        "trafo_sn_mva" : session.get('trafo_sn_mva'),
        }

    return trans_json

@bp.route('/urbs/process_profiles', methods=['GET', 'POST'])
def formatProcessSetup():
    pro_prop = pd.read_csv(os.path.join(os.getcwd(), 'pandapower2urbs_dataset_template/dataset/process/pro_prop.csv'), sep=',')
    pro_com_prop = pd.read_csv(os.path.join(os.getcwd(),'pandapower2urbs_dataset_template/dataset/process/pro_com_prop.csv'), sep=',')
    process_json = {
        "pro_prop" : pro_prop.to_json(),
        "pro_com_prop" : pro_com_prop.to_json(),
        }

    return process_json

@bp.route('/urbs/storage_profiles', methods=['GET', 'POST'])
def formatStorageSetup():
    sto_prop = pd.read_csv(os.path.join(os.getcwd(), 'pandapower2urbs_dataset_template/dataset/storage/sto_prop.csv'), sep=',')
    storage_json = {
        "sto_prop" : sto_prop.to_json(),
        }

    return storage_json

@bp.route('/urbs/commodity_profiles', methods=['GET', 'POST'])
def formatCommoditySetup():
    com_prop = pd.read_csv(os.path.join(os.getcwd(), 'pandapower2urbs_dataset_template/dataset/commodity/com_prop.csv'), sep=',')
    com_prop.replace([np.inf, -np.inf], "inf", inplace=True)
    commodity_json = {
        "com_prop" : com_prop.to_json(),
        }

    return commodity_json

@bp.route('/urbs/supim_profiles', methods=['GET', 'POST'])
def supimProfiles():
    supim_solar = pd.read_csv(os.path.join(os.getcwd(), 'pandapower2urbs_dataset_template/dataset/supim/profiles/solar.csv'), sep=',')
    supim_solar = supim_solar.fillna(0)
    supim_solar.to_csv('pandapower2urbs\\dataset\\supim\\profiles\\solar.csv', index=False)
    supim_json = {"solar" : supim_solar.to_json(),
                }

    return supim_json

@bp.route('/urbs/timevareff_profiles', methods=['GET', 'POST'])
def timevareffProfiles():
    charging_station = pd.read_csv(os.path.join(os.getcwd(), 'pandapower2urbs_dataset_template/dataset/timevareff/profiles/charging_station.csv'), sep=',')
    heatpump_air = pd.read_csv(os.path.join(os.getcwd(),'pandapower2urbs_dataset_template/dataset/timevareff/profiles/heatpump_air.csv'), sep=',')
    heatpump_air_heizstrom = pd.read_csv(os.path.join(os.getcwd(),'pandapower2urbs_dataset_template/dataset/timevareff/profiles/heatpump_air_heizstrom.csv'), sep=',')

    timevareff_json = {
            "charging_station" : charging_station.to_json(),
            "heatpump_air" : heatpump_air.to_json(),
            "heatpump_air_heizstrom" : heatpump_air_heizstrom.to_json()
            }

    return timevareff_json


#help function that returns all the index positions of a char in a list
#used to find which flags are set to 1 in the demand, supim, timevareff checkbox editors and correctly write to css
def find(s, ch):
    return [i for i, ltr in enumerate(s) if ltr == ch]

def createCSVFromCheckboxes (json_data, columns): 
    conf = []

    for bus in json_data:
        current_row = []
        current_row.append(bus)
        for profile in json_data[bus]:
            chosen_profiles = find(profile, '1')
            chosen_profiles_str = ''
            if chosen_profiles:
                chosen_profiles_str = ';'.join(str(p) for p in chosen_profiles)
            else:
                chosen_profiles_str = str(len(profile) - 1)
            current_row.append(chosen_profiles_str)
        conf.append(current_row)
    
    demand_df = pd.DataFrame(conf, columns=columns)
    return demand_df

#the js returns the aggregated profiles the user chose for load bus and each type of demand
#the returned datastructure is changed into a csv that fits the demanded format
@bp.route('/urbs/demand_csv_setup', methods=['GET', 'POST'])
def formatDemandCSV():
    if request.method == 'POST':
        demand_profiles = ["site","electricity","electricity-reactive","mobility","space_heat","water_heat"]
        demand_df = createCSVFromCheckboxes(request.get_json(), demand_profiles)
        demand_df.to_csv('pandapower2urbs\\dataset\\demand\\demand_conf.csv', index=False)
        return 'Success', 200

@bp.route('/urbs/buildings_csv_setup', methods=['GET', 'POST'])
def formatBuildingsCSV():
    if request.method == 'POST':
        buildings_user_data = json.loads(request.get_json())

        buildings_df_columns =['bid','footprint_area', 'PV_cap_kW','use', 'free_walls', 'floors', 'dwellings', 'occupants','ref_level_roof', 'ref_level_wall', 'ref_level_floor','ref_level_window']      
        plz = session.get('plz')['value']
        plz_version = session['plz_version']
        gg = GridGenerator(plz=plz, version_id=plz_version)
        pg = gg.pgr

        buildings_osm_id_list = []

        for entry in buildings_user_data:
            buildings_osm_id = pg.test__getBuildingOfBus(plz, entry['lat'], entry['lon'])
            
            if buildings_osm_id:
                buildings_osm_id_list.append(buildings_osm_id)
            else:
                buildings_osm_id_list.append("")

        buildings_data_aggregator = []
        bid = 1
        for osm_id in buildings_osm_id_list:
            if osm_id != "":
                additional_data = pg.test_getAdditionalBuildingData(osm_id)
                buildings_data_aggregator.append([bid] + additional_data)
            else:
                additional_data = [""] * 11
                buildings_data_aggregator.append([bid] + additional_data)
            bid += 1

        buildings_data = pd.DataFrame(buildings_data_aggregator,columns=buildings_df_columns)
        buildings_data = buildings_data.join(pd.DataFrame.from_dict(buildings_user_data))
        buildings_conf = buildings_data.filter(['name', 'bid'], axis='columns').copy()
        buildings_conf = buildings_conf.rename(columns={'name' : 'urbs_name', 'bid' : 'building_id'})
        buildings_data = buildings_data.drop('name', axis='columns')
 
        buildings_data.to_csv('pandapower2urbs\\dataset\\_buildings\\building_data.csv', index=False)
        buildings_conf.to_csv('pandapower2urbs\\dataset\\_buildings\\building_conf.csv', index=False)
        return 'Success', 200

@bp.route('/urbs/transmission_csv_setup', methods=['GET', 'POST'])
def formatTransmissionCSV():
    if request.method == 'POST':
        trans_data = request.get_json()
        for table in trans_data:
            columns = []
            data = []
            for feature in trans_data[table]:
                row = []
                if table == 'voltage_limits':
                    columns.append(feature)
                    data.append(trans_data[table][feature])
                else:
                    columns = ['id']
                    row.append(feature)
                    for feature_elem in trans_data[table][feature]:
                        if feature_elem not in columns:
                            columns.append(feature_elem)
                        row.append(trans_data[table][feature][feature_elem])
                    data.append(row)
            if table == 'voltage_limits':
                data = [data]
            trans_data_df = pd.DataFrame(data, columns=columns)
            trans_data_df.to_csv('pandapower2urbs\\dataset\\_transmission\\' + table  + '.csv', index=False)
        return 'Success', 200

@bp.route('/urbs/global_csv_setup', methods=['GET', 'POST'])
def formatGlobalCSV():
    if request.method == 'POST':
        global_columns = ['Property', 'value']
        global_conf = []
        global_json = json.loads(request.get_json())
        for key in global_json:
            global_conf.append([key, global_json[key]])

        global_df = pd.DataFrame(global_conf, columns=global_columns)
        global_df.to_csv('pandapower2urbs\\dataset\\global\\global.csv', index=False)

        return 'Success', 200

@bp.route('/urbs/commodity_csv_setup', methods=['GET', 'POST'])
def formatCommodityCSV():
    if request.method == 'POST':
        commodity_data = json.loads(request.get_json())
        columns = ['name']
        data = []
        for commodity in commodity_data:
            row = [commodity]
            for feature in commodity_data[commodity]:
                if feature not in columns:
                    columns.append(feature)
                row.append(commodity_data[commodity][feature])
            data.append(row)
        comm_df = pd.DataFrame(data, columns=columns)
        comm_df.to_csv('pandapower2urbs\\dataset\\commodity\\com_prop.csv', index=False)
        return 'Success', 200


@bp.route('/urbs/process_csv_setup', methods=['GET', 'POST'])
def formatProcessCSV():
    if request.method == 'POST':
        #combine with sto_conf into single method
        process_data = request.get_json()
        pro_conf_df = pd.read_json(process_data['pro_conf'], orient='split')
        #pro_conf_df = pro_conf_df.replace("", "0")
        pro_conf_df = pro_conf_df[:-1]
        pro_conf_df.to_csv('pandapower2urbs\\dataset\\process\\pro_conf.csv', index=False)

        columns = ['name']
        data = []
        pro_prop_data = json.loads(process_data['pro_prop'])
        for pro_prop in pro_prop_data:
            row = [pro_prop]
            for feature in pro_prop_data[pro_prop]:
                if feature not in columns:
                    columns.append(feature)
                row.append(pro_prop_data[pro_prop][feature])
            data.append(row)
        pro_prop_df = pd.DataFrame(data, columns=columns)
        pro_prop_df.to_csv('pandapower2urbs\\dataset\\process\\pro_prop.csv', index=False)

        columns = ['Process', 'Commodity', 'Direction']
        data = []
        pro_com_prop_data = json.loads(process_data['pro_com_prop'])

        for process in pro_com_prop_data:
            for direction in pro_com_prop_data[process]:
                if pro_com_prop_data[process][direction]:
                    row = []
                    for com_prop in pro_com_prop_data[process][direction]:
                        row = [process, com_prop, direction]
                        for feature in pro_com_prop_data[process][direction][com_prop]:
                            if feature not in columns:
                                columns.append(feature)
                            row.append(pro_com_prop_data[process][direction][com_prop][feature])
                    data.append(row)
        pro_com_prop_df = pd.DataFrame(data, columns=columns)
        pro_com_prop_df.to_csv('pandapower2urbs\\dataset\\process\\pro_com_prop.csv', index=False)
        return 'Success', 200

@bp.route('/urbs/storage_csv_setup', methods=['GET', 'POST'])
def formatStorageCSV():
    if request.method == 'POST':
        storage_data = request.get_json()
        sto_conf_df = pd.read_json(storage_data['sto_conf'], orient='split')
        sto_conf_df = sto_conf_df.replace("", "0")
        sto_conf_df = sto_conf_df[:-1]
        sto_conf_df.to_csv('pandapower2urbs\\dataset\\storage\\sto_conf.csv', index=False)

        #combine with com_prop, pro_prop into single method
        sto_prop_data = json.loads(storage_data['sto_prop'])
        columns = ['name']
        data = []
        for sto_prop in sto_prop_data:
            row = [sto_prop]
            for feature in sto_prop_data[sto_prop]:
                if feature not in columns:
                    columns.append(feature)
                row.append(sto_prop_data[sto_prop][feature])
            data.append(row)
        sto_prop_df = pd.DataFrame(data, columns=columns)
        sto_prop_df.to_csv('pandapower2urbs\\dataset\\storage\\sto_prop.csv', index=False)
        
        return 'Success', 200
    
@bp.route('/urbs/supim_csv_setup', methods=['GET', 'POST'])
def formatSupimCSV():
    if request.method == 'POST':
        supim_profiles = ["site","solar"]
        supim_df = createCSVFromCheckboxes(request.get_json(), supim_profiles)
        supim_df.to_csv('pandapower2urbs\\dataset\\supim\\supim_conf.csv', index=False)
        return 'Success', 200

@bp.route('/urbs/timevareff_csv_setup', methods=['GET', 'POST'])
def formatTimevareffCSV():
    if request.method == 'POST':
        timevareff_profiles = ["site","charging_station","heatpump_air","heatpump_air_heizstrom"]
        timevareff_df = createCSVFromCheckboxes(request.get_json(), timevareff_profiles)
        timevareff_df.to_csv('pandapower2urbs\\dataset\\timevareff\\timevareff_conf.csv', index=False)

        return 'Success', 200

import subprocess

def switch_conda_environment(env_path, env_name):
    cmd = f'cd {env_path} && conda run -n {env_name} python.exe run_automatedoutput.py'
    urbs_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, universal_newlines=True, shell=True)
    for stdout_line in iter(urbs_process.stdout.readline, ""):
        yield stdout_line 
    urbs_process.stdout.close()
    return_code = urbs_process.wait()
    if return_code:
        raise subprocess.CalledProcessError(return_code, cmd)
    print(f"Switched to conda environment: {env_name}")

@bp.route('/urbs/pdp2Urbs', methods=['GET', 'POST'])
def runPdp2Urbs():
    pp2u.convertPandapower2Urbs()
    cmd = f'cd {URBS_RUN_FILE_PATH} && conda run -n {URBS_CONDA_ENV_NAME} python.exe run_automatedoutput.py'
    urbs_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, universal_newlines=True, shell=True)
    for stdout_line in iter(urbs_process.stdout.readline, ""):
        yield stdout_line 
    urbs_process.stdout.close()
    return_code = urbs_process.wait()
    if return_code:
        raise subprocess.CalledProcessError(return_code, cmd)
    print(f"Switched to conda environment: {URBS_CONDA_ENV_NAME}")
    
    return 'Success', 200

