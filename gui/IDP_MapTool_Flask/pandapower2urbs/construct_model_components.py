import os

from pandapower2urbs.config import LOCAL_PATH, SAVE_PATH, PANDAPOWER_PATH, VOLTAGE_LIMITS_PATH, BUILDING_LIST_PATH, \
                   CABLE_DATA_PATH, TRAFO_DATA_PATH, \
                   BUILDING_CONF_PATH, GLOBAL_PROP_PATH, COM_PROP_PATH, PRO_CONF_PATH, \
                   PRO_PROP_PATH, PRO_COM_PROP_PATH, STO_CONF_PATH, STO_PROP_PATH
import pandas as pd
import pandapower as pp
import numpy as np
import warnings

warnings.simplefilter(action='ignore', category=pd.errors.PerformanceWarning)
warnings.simplefilter(action='ignore', category=FutureWarning)

def get_building_data(building_data_file_path, selected_buildings_path):
    # read building data from imported file
    building_data_ori = pd.read_csv(building_data_file_path, sep=',')
    selected_buildings = pd.read_csv(selected_buildings_path, sep=',')
    bui_id = selected_buildings['building_id'].tolist()
    building_data = []
    for id in bui_id:
        building_data.append(building_data_ori.iloc[id-1])
    building_data_df = pd.DataFrame(building_data)
    return building_data_df
    
    
def convertPandapower2Urbs():
        ##### read the model-forming data
    building_conf = pd.read_csv(BUILDING_CONF_PATH).set_index('urbs_name')
    building_data = get_building_data(BUILDING_LIST_PATH, BUILDING_CONF_PATH).set_index('bid')

    pp_grid = pp.from_excel(PANDAPOWER_PATH)
    voltage_limits_df = pd.read_csv(VOLTAGE_LIMITS_PATH, sep=',')
    rated_voltage_kw = min(pp_grid.bus.vn_kv)

    global_df = pd.read_csv(GLOBAL_PROP_PATH, sep=',').set_index('Property')
    com_prop = pd.read_csv(COM_PROP_PATH, sep=',')

    ##### generate Site sheet
    # identify the bus index and label where the transformer is located
    test_var = pp_grid.trafo.hv_bus.iloc[0]
    trafo_idx  = pp_grid.trafo.hv_bus.iloc[0] # 0
    trafo_name = pp_grid.bus.name[trafo_idx] # Trafostation_OS
    print("Start generating Site Sheet")
    print("identified trafo bus index and label")

    # identify the bus index and label where the main busbar is located
    mainbusbar_idx  = pp_grid.trafo.lv_bus.iloc[0]               #1
    mainbusbar_name = pp_grid.bus.name[mainbusbar_idx] #main_busbar

    print("identified main bus bar index and label")

    # identify the load buses, where the demands are defined (buildings)
    loadbus_idx =  pp_grid.bus[pp_grid.bus.index.isin(pp_grid.load.bus)].index
    loadbus_name = pp_grid.bus[pp_grid.bus.index.isin(pp_grid.load.bus)].name

    print("identified load bus indexes and labels")

    # identify intermediate (branching) buses, where no demand is located
    intbus_idx  = pp_grid.bus[~pp_grid.bus.index.isin([trafo_idx]+
                                                     [mainbusbar_idx]+
                                                     pp_grid.load.bus.to_list())].index
    intbus_name = pp_grid.bus[~pp_grid.bus.index.isin([trafo_idx]+
                                                     [mainbusbar_idx]+
                                                     pp_grid.load.bus.to_list())].name

    print("identified intermediate bus indexes and labels")

    ##### construct Site sheet
    site_tuple = []
    site_tuple.append((trafo_name,      None, rated_voltage_kw, None, 1, 1, None))
    site_tuple.append((mainbusbar_name, None, rated_voltage_kw, 1,    1, 1, None))

    for loadbus in loadbus_name:
        site_tuple.append((loadbus, None, rated_voltage_kw,  None,
                           voltage_limits_df['v_min'][0], voltage_limits_df['v_max'][0], global_df.loc['power_price_kw'][0]))
    for intbus in intbus_name:
        site_tuple.append((intbus, None, rated_voltage_kw,  None,
                           voltage_limits_df['v_min'][0], voltage_limits_df['v_max'][0], None))

    site_df = pd.DataFrame(site_tuple, columns=['Name', 'area', 'base-voltage', 'ref-node',
                                                'min-voltage', 'max-voltage', 'power_price_kw'])

    print("Finished generating Site Sheet")
    print("Start generating Demand Sheet")
    ##### construct Demand sheet
    nr_of_cars = {} # Number of electric cars each building

    demand_df = pd.DataFrame()
    demand_df.index.name = 't' # time steps as index
    demand_conf = pd.read_csv(os.path.join(LOCAL_PATH,'./dataset/demand/demand_conf.csv'),sep=',').set_index('site')  # read demand conf file

    # for each commodity demanded, read the available profiles
    for demand_com in demand_conf.columns:
        demand_com_profiles = pd.read_csv(os.path.join(LOCAL_PATH,'./dataset/demand/profiles/{}.csv').format(demand_com),sep=',').set_index('t')

        for demand_sit in demand_conf.index:
            # for the given demand commodity, scan through all the sites to see if they are involved
            if pd.isna(demand_conf.loc[demand_sit][demand_com]):
                if demand_com == 'mobility':
                    nr_of_cars[demand_sit] = 0
                continue
            else:
                # demand exists in site for the com. collect all profiles. For multiple profiles separated with semicolons:
                #   if com != mobility: sum them up,
                #   if com == mobility: define each car demand separately
                profile_idcs = str(demand_conf.loc[demand_sit][demand_com]).split(';')
                if demand_com == 'mobility':
                    nr_of_cars[demand_sit] = len(profile_idcs)
                    for car, profile_idx in enumerate(profile_idcs):
                        try:
                            demand_df[demand_sit+'.'+demand_com+str(car+1)] = demand_com_profiles[profile_idx]
                        except:
                            demand_df[demand_sit+'.'+demand_com+str(car+1)] = demand_com_profiles[str(profile_idx)]
                else: #mobility
                    demand = pd.Series([0] * 8761)
                    for profile_idx in profile_idcs:
                        try:
                            demand += demand_com_profiles[profile_idx]
                        except:
                            demand += demand_com_profiles[str(profile_idx)]
                    demand_df[demand_sit+'.'+demand_com] = demand
    demand_df.loc[0] = 0 # add a zero demand for the non-modelled zeroth time step
    demand_df["weight_typeperiod"] = "" # add a required empty column for the typical period weights
    demand_df.sort_index(inplace=True)

    print("Finished generating Demand Sheet")
    print("Start generating Supim Sheet")

    ##### Construct Supim sheet (e.g. hourly solar capacity factor)
    supim_df = pd.DataFrame()
    supim_df.index.name = 't'
    supim_conf = pd.read_csv(os.path.join(LOCAL_PATH,'./dataset/supim/supim_conf.csv'),sep=',').set_index('site') # read configuration file
    for supim_com in supim_conf.columns:
        # for each Supim commodity, read the available profiles
        supim_com_profiles = pd.read_csv(os.path.join(LOCAL_PATH,'./dataset/supim/profiles/{}.csv').format(supim_com),sep=',').set_index('t')
        for supim_sit in supim_conf.index:
            #for each site involved, see if a Supim profile is assigned for any given Supim commmodity
            if not pd.isna(supim_conf.loc[supim_sit][supim_com]):
                profile_idx = supim_conf.loc[supim_sit][supim_com]
                try:
                    supim_df[supim_sit+'.'+supim_com] = supim_com_profiles[profile_idx]
                except:
                    supim_df[supim_sit+'.'+supim_com] = supim_com_profiles[str(profile_idx)]
    supim_df.loc[0] = 0
    supim_df.sort_index(inplace=True)

    print("Finished generating Supim Sheet")
    print("Start generating Timevareff Sheet")

    # add timevareff sheet (e.g. heat pump cop/car availability)
    timevareff_df = pd.DataFrame()
    timevareff_df.index.name = 't'
    timevareff_conf = pd.read_csv(os.path.join(LOCAL_PATH,'./dataset/timevareff/timevareff_conf.csv'),sep=',').set_index('site')
    for timevareff_pro in timevareff_conf.columns:
        timevareff_pro_profiles = pd.read_csv(os.path.join(LOCAL_PATH,'./dataset/timevareff/profiles/{}.csv').format(timevareff_pro),sep=',').set_index('t')
        for timevareff_sit in timevareff_conf.index:
            if not pd.isna(timevareff_conf.loc[timevareff_sit][timevareff_pro]):
                # availability of multiple charging stations in each building are defined separately
                # (for charging_station1, charging_station2, ...)
                if timevareff_pro == 'charging_station':
                    profile_idcs = str(timevareff_conf.loc[timevareff_sit][timevareff_pro]).split(';')
                    for car, profile_idx in enumerate(profile_idcs):
                        try:
                            timevareff_df[timevareff_sit+'.'+timevareff_pro+str(car+1)] = timevareff_pro_profiles[profile_idx]
                        except:
                            timevareff_df[timevareff_sit+'.'+timevareff_pro+str(car+1)] = timevareff_pro_profiles[str(profile_idx)]
                else:
                    profile_idx = timevareff_conf.loc[timevareff_sit][timevareff_pro]
                    try:
                        timevareff_df[timevareff_sit+'.'+timevareff_pro] = timevareff_pro_profiles[profile_idx]
                    except:
                        timevareff_df[timevareff_sit+'.'+timevareff_pro] = timevareff_pro_profiles[str(profile_idx)]

    timevareff_df.loc[0] = 0
    timevareff_df.sort_index(inplace=True)

    ##### Construct the Buy-Sell-Price multipliers (default: all ones)
    buysellprice_df = pd.read_csv(os.path.join(LOCAL_PATH,'./dataset/buysellprice/buysellprice.csv'),sep=',').set_index('t')
    buysellprice_df.loc[0] = 0
    buysellprice_df.sort_index(inplace=True)

    print("Finished generating Timevareff Sheet")
    print("Start generating Process-Commodity Sheet")

    ##### Construct the Process-Commodity sheet
    process_commodity_df = pd.read_csv(PRO_COM_PROP_PATH, sep=',')
    if process_commodity_df.Process.isin(['charging_station']).any():
        #create entries for multiple charging station processes (depending on the maximum number of cars over each building)
        max_cars = int(max(nr_of_cars.values()))
        for car in range(1,max_cars+1):
            cs = process_commodity_df[process_commodity_df.Process == 'charging_station']
            in_ = tuple(cs[cs.Direction == 'In'].to_numpy()[0])
            out_ = tuple(cs[cs.Direction == 'Out'].to_numpy()[0])
            process_commodity_df=process_commodity_df.append({'Process': in_[0]+str(car),
                                         'Commodity': in_[1],
                                         'Direction':in_[2],
                                         'ratio': in_[3],
                                         'ratio-min':in_[4]}, ignore_index = True)
            process_commodity_df=process_commodity_df.append({'Process': out_[0]+str(car),
                                         'Commodity': out_[1]+str(car),
                                         'Direction':out_[2],
                                         'ratio': out_[3],
                                         'ratio-min':out_[4]}, ignore_index = True)

    # drop the charging_station entries that have no integer index
    process_commodity_df.drop(process_commodity_df[process_commodity_df.Process == 'charging_station'].index, 
                              inplace=True)

    print("Finished generating Process-Commodity  Sheet")
    print("Start generating Process Sheet")

    ##### Construct the Process sheet
    pro_conf = pd.read_csv(PRO_CONF_PATH, sep=',').set_index('urbs_name')
    pro_prop = pd.read_csv(PRO_PROP_PATH, sep=',').set_index('name')
    process_tuple = []
    for sit in pro_conf.index:
        for pro in pro_conf.columns:
            # For each site and process, check if the process is defined
            # empty cell: process neither existing nor possible to build,
            #          0: process does not exist but can be built,
            #         >0: process exists with the given capacity)

            if not np.isnan(pro_conf.loc[sit][pro]):
                # 'cap_up' column in 'pro_prop' defines rules for the max. allowed capacity for a process
                #     empty cell: process can be expanded indefinitely
                #    '%inst_cap': process cannot be expanded beyond its existing capacity
                #   '%roof_area': process cannot be expanded beyond the rooftop area of the building (relevant for PV)
                #         '*XXX': process can be expanded up to a given multiple of the existing capacity (XXX float)
                if pro_prop.loc[pro].cap_up == '%inst_cap':
                    capup = pro_conf.loc[sit][pro]
                elif pro_prop.loc[pro].cap_up == '%roof_area':
                    capup = building_data.loc[building_conf.loc[sit].building_id].PV_cap_kW
                elif str(pro_prop.loc[pro].cap_up).startswith('*') and pro_conf.loc[sit][pro] > 0:
                    capup = float(pro_prop.loc[pro].cap_up[1:]) * pro_conf.loc[sit][pro]
                else:
                    capup = 'inf'

                # if process == charging_station, define as many processes as the number of cars in the building, with
                # distinct integer indices (charging_station1, charging_station2, ....)
                if pro == 'charging_station':            
                    for car in range(1, nr_of_cars[sit] + 1):
                        process_tuple.append((sit, pro + str(car), 
                                       pro_conf.loc[sit][pro], 0, capup,) +
                                       tuple(pro_prop.loc[pro].iloc[1:].tolist()) )
                else:
                    process_tuple.append((sit, pro, 
                                       pro_conf.loc[sit][pro], 0, capup,) +
                                       tuple(pro_prop.loc[pro].iloc[1:].tolist()) )
    process_df = pd.DataFrame(process_tuple,
                              columns=['Site','Process','inst-cap','cap-lo',
                                       'cap-up','ramp-up-grad','ramp-down-grad',
                                       'min-fraction','start-time','inv-cost',
                                       'fix-cost','var-cost','wacc','depreciation',
                                       'area-per-cap','cap-block','start-price',
                                       'on-off','pf-min','decommissionable',
                                       'decom-saving','inv-cost-fix'])

    ##### Construct the Process sheet
    commodity_tuple = []

    print("Add commodities")

    ## Add electrical commodities for all grid buses
    # electricity (Active power flow)
    # electricity_hp (Heizstrom)
    # electricity-reactive (Reactive power flow)

    elec_coms_ = com_prop.set_index('name')
    elec_coms = elec_coms_[elec_coms_.electrical == 1] # identify electrical commodities

    for elec_com in elec_coms.index:
        for elec_bus in [trafo_name, mainbusbar_name] + loadbus_name.to_list() + intbus_name.to_list():
            if elec_bus == trafo_name and elec_com == 'electricity-reactive': # reactive power is not defined for trafobus
                continue
            if elec_bus in loadbus_name.to_list():
                commodity_tuple.append((elec_bus, 
                                        elec_com,
                                        elec_coms.loc[elec_com]['type'], 
                                        elec_coms.loc[elec_com].price,
                                        elec_coms.loc[elec_com]['max'],
                                        elec_coms.loc[elec_com].max_per_hour))
            else:
                commodity_tuple.append((elec_bus,
                                        elec_com,           
                                        'Stock', 
                                        elec_coms.loc[elec_com].price,
                                        elec_coms.loc[elec_com]['max'],
                                        elec_coms.loc[elec_com].max_per_hour)) 

    ## add process related commodities
    for sit in pro_conf.index:
        for pro in pro_conf.columns:
            # scroll through all process and sites, and only define the commodities that are dealt with the processes
            # existing in each site (by referring to process-commodity sheet)

            if not np.isnan(pro_conf.loc[sit][pro]):
                # for charging_station process, define as many commodities as the number of cars in a given building

                if pro == 'charging_station':
                    coms_to_add = process_commodity_df[
                                 process_commodity_df.Process.str.startswith(pro)].Commodity            
                    for car in range(1, nr_of_cars[sit]+1):
                        for com in coms_to_add:
                            if 'mobility' in com:
                                mobility_com = com_prop[com_prop.name == 'mobility'].iloc[:,:-1].to_numpy()[0]
                                mobility_com[0] = mobility_com[0] + str(car)                            
                                commodity_tuple.append((sit,) + tuple(mobility_com))
                else:
                    coms_to_add = process_commodity_df[
                                 process_commodity_df.Process == pro].Commodity
                    for com in coms_to_add:
                        if com not in elec_coms.index:
                            commodity_tuple.append((sit,) + tuple(com_prop[com_prop.name == com].iloc[:,:-1].to_numpy()[0]))
    commodity_df = pd.DataFrame(commodity_tuple, columns=['Site', 'Commodity', 'Type', 'price', 'max', 'maxperhour'])
    commodity_df.drop_duplicates(inplace=True)

    print("Finished generating Process  Sheet")
    print("Start generating Transmission Sheet")

    ##### generate the Transmission sheet
    transmission_tuple = []

    ## Add possible transformers
    # kontXXX: already installed conventional transformer (konventioneller Ortsnetztransformator)  with XXX = cap. in kVA
    # rontXXX: replacement options to controllable transformers (regelbarer Ortsnetztransformator) with XXX = cap. in kVA
    trafo_data = pd.read_csv(TRAFO_DATA_PATH,sep=',').set_index('id')
    built_kont_size = pp_grid.trafo.iloc[0].sn_mva * 1000 # read the capacity of existing trafo (kont) from pandapower file
 
    print("Start adding Trafo entries")

    # add transformer entries
    for trafo in trafo_data.index:
        trafo_sit_in = trafo_name
        trafo_sit_out = mainbusbar_name
        trafo_com = 'electricity'
        trafo_eff = 1
        trafo_cap_lo = 0
        trafo_reactance = ''
        trafo_resistance = ''
        trafo_difflimit = ''
        trafo_base_voltage = ''
        if trafo[0:4] == 'kont' and trafo_data.loc[trafo]['cap'] == built_kont_size:
            trafo_inst_cap = trafo_data.loc[trafo]['cap']
            trafo_decommissionable = 1
            trafo_decom_saving = 0
        else:
            trafo_inst_cap = 0
            trafo_decommissionable = ''
            trafo_decom_saving = ''   
        
        transmission_tuple.append((trafo_sit_in, 
                                   trafo_sit_out, 
                                   trafo, 
                                   trafo_com, 
                                   trafo_eff,    
                                   0.5 * trafo_data.loc[trafo]['inv-cost']/trafo_data.loc[trafo]['cap'],
                                   trafo_data.loc[trafo]['fix-cost'],
                                   trafo_data.loc[trafo]['var-cost'],
                                   trafo_inst_cap,
                                   trafo_cap_lo,
                                   trafo_data.loc[trafo]['cap'],
                                   trafo_data.loc[trafo]['wacc'],
                                   trafo_data.loc[trafo]['depreciation'],
                                   trafo_reactance,
                                   trafo_resistance,
                                   trafo_difflimit,
                                   trafo_base_voltage,
                                   trafo_data.loc[trafo]['cap'],
                                   trafo_decommissionable,
                                   trafo_decom_saving))
        transmission_tuple.append((trafo_sit_out, 
                                   trafo_sit_in, 
                                   trafo, 
                                   trafo_com, 
                                   trafo_eff,    
                                   0.5 * trafo_data.loc[trafo]['inv-cost']/trafo_data.loc[trafo]['cap'],
                                   trafo_data.loc[trafo]['fix-cost'],
                                   trafo_data.loc[trafo]['var-cost'],
                                   trafo_inst_cap,
                                   trafo_cap_lo,
                                   trafo_data.loc[trafo]['cap'],
                                   trafo_data.loc[trafo]['wacc'],
                                   trafo_data.loc[trafo]['depreciation'],
                                   trafo_reactance,
                                   trafo_resistance,
                                   trafo_difflimit,
                                   trafo_base_voltage,
                                   trafo_data.loc[trafo]['cap'],
                                   trafo_decommissionable,
                                   trafo_decom_saving))         
    

    print("Start adding cable entries")

    # add cable entries
    cable_data = pd.read_csv(CABLE_DATA_PATH,sep=',').set_index('id')

    for line in pp_grid.line.index: # go through all the cable sections in the grid
        built_parallel = pp_grid.line.loc[line].parallel # current number of parallel cables for a given section
        reinforcements = [0, 1] # options to add one more parallel cable (add 2 to the list, if two more cables are allowed)

        # check the cost parameters for the cable type in the given section
        if pp_grid.line.loc[line].std_type in cable_data.index:
            cable_type_for_params = pp_grid.line.loc[line].std_type
        else:
            cable_type_for_params = '_others' # cost parameters for _others taken if no data exists for given cable type

        # add cable entries for each reinforcement option (*1 (1+0), *2 (1+1) ...)
        for rein in reinforcements:
            line_sit_in = pp_grid.bus.loc[pp_grid.line.loc[line].from_bus]['name']
            line_sit_out = pp_grid.bus.loc[pp_grid.line.loc[line].to_bus]['name']
            line_transmission = pp_grid.line.loc[line].std_type + '*' + str(built_parallel + rein) # line label (*1, *2...)
            line_com = 'electricity'
            line_eff = 1
            if rein == 0:
                line_inv_cost = 0
            else:
                # cost for a cable section = (digging_perkm + material_perkm * rein) * km / capkVA
                # (capKVA = max_loading * sqrt(3) * I_max * V_rated)
                line_inv_cost = ((cable_data.loc[cable_type_for_params].material_cost * rein +
                                 cable_data.loc[cable_type_for_params].digging_cost) *
                                 pp_grid.line.loc[line].length_km /
                                 (cable_data.loc[cable_type_for_params].loading_limit * 
                                  (built_parallel + rein) * pp_grid.line.loc[line].max_i_ka * 
                                  1.732 * rated_voltage_kw * 1000))
            # no fix and variable costs defined for cables
            line_fix_cost = 0
            line_var_cost = 0
            if rein == 0: # currently installed capacity exists for the non-reinforcement entry
                line_inst_cap = (cable_data.loc[cable_type_for_params].loading_limit * 
                                (built_parallel + rein) * pp_grid.line.loc[line].max_i_ka * 
                                 1.732 * rated_voltage_kw * 1000)
            else: # currently installed capacity is zero for the reinforcement entry
                line_inst_cap = 0
            line_cap_lo = 0

            #block investment: cap_up = cap_block = power flow capacity of the cable bundle
            line_cap_up = (cable_data.loc[cable_type_for_params].loading_limit *
                                (built_parallel + rein) * pp_grid.line.loc[line].max_i_ka * 
                                 1.732 * rated_voltage_kw * 1000)

            line_wacc = cable_data.loc[cable_type_for_params].wacc
            print(cable_type_for_params, cable_data.loc[cable_type_for_params]['deprecation'])
            line_depreciation = cable_data.loc[cable_type_for_params]['deprecation']

            #reactance (or resistance) of a bundle with N cables = reactance (or resistance) of one cable / N
            line_reactance = (pp_grid.line.loc[line].x_ohm_per_km * pp_grid.line.loc[line].length_km /
                              (built_parallel + rein))
            line_resistance = (pp_grid.line.loc[line].r_ohm_per_km * pp_grid.line.loc[line].length_km /
                              (built_parallel + rein))
            line_difflimit = ''
            line_base_voltage = ''
            line_tra_block = line_cap_up
            if rein == 0:
                line_decommissionable = 1
                line_decomsaving = 0
            else:
                line_decommissionable = ''
                line_decomsaving = ''
            transmission_tuple.append((line_sit_in, 
                                       line_sit_out, 
                                       line_transmission, 
                                       line_com, 
                                       line_eff,    
                                       line_inv_cost,
                                       line_fix_cost,
                                       line_var_cost,
                                       line_inst_cap,
                                       line_cap_lo,
                                       line_cap_up,
                                       line_wacc,
                                       line_depreciation,
                                       line_reactance,
                                       line_resistance,
                                       line_difflimit,
                                       line_base_voltage,
                                       line_tra_block,
                                       line_decommissionable,
                                       line_decomsaving))       
    transmission_df = pd.DataFrame(transmission_tuple, columns=['Site In', 'Site Out', 'Transmission', 'Commodity',
                                                                'eff',	'inv-cost',	'fix-cost', 'var-cost',	'inst-cap',
                                                                'cap-lo',	'cap-up',	'wacc',	'depreciation', 'reactance',
                                                                'resistance',	'difflimit',	'base_voltage',	'tra-block',
                                                                'decommissionable',	'decom-saving'])

    print("Start generating Storage Sheet Tuple")

    # generate storage sheet tuple
    sto_conf = pd.read_csv(STO_CONF_PATH, sep=',').set_index('urbs_name')
    sto_prop = pd.read_csv(STO_PROP_PATH, sep=',').set_index('name')
    storage_tuple = []
    sto_conf.columns = [(sto,par) for (sto, par) in sto_conf.columns.str.split('.')]
    unique_sto = set([sto for (sto, par) in sto_conf.columns])

    for sit in sto_conf.index:
        for sto in unique_sto:
            if not np.isnan(sto_conf.loc[sit][(sto,'c')]):
                if sto_prop.loc[sto].cap_up == '%inst_cap':
                    capup_c = sto_conf.loc[sit][(sto,'c')]
                    capup_p = sto_conf.loc[sit][(sto,'p')]
                elif str(sto_prop.loc[sto].cap_up).startswith('*') and sto_conf.loc[sit][(sto,'c')] > 0:
                    capup_c = float(sto_prop.loc[sto].cap_up[1:]) * sto_conf.loc[sit][(sto,'c')]
                    capup_p = float(sto_prop.loc[sto].cap_up[1:]) * sto_conf.loc[sit][(sto,'p')]
                else:
                    capup_c = 'inf'
                    capup_p = 'inf'
                
                if sto == 'mobility_storage':            
                    for car in range(1, nr_of_cars[sit] + 1):
                        storage_tuple.append((sit, sto + str(car), sto_prop.loc[sto].commodity + str(car), 
                                              sto_conf.loc[sit][(sto,'c')], 0, capup_c, 
                                              sto_conf.loc[sit][(sto,'p')], 0, capup_p,) +
                                              tuple(sto_prop.loc[sto].iloc[2:].tolist()) )
                else:
                    storage_tuple.append((sit, sto, sto_prop.loc[sto].commodity, 
                                          sto_conf.loc[sit][(sto,'c')], 0, capup_c, 
                                          sto_conf.loc[sit][(sto,'p')], 0, capup_p,) +
                                          tuple(sto_prop.loc[sto].iloc[2:].tolist()) )
                                       
    storage_df = pd.DataFrame(storage_tuple, columns=['Site', 'Storage', 'Commodity', 'inst-cap-c', 'cap-lo-c', 'cap-up-c', 'inst-cap-p', 'cap-lo-p', 'cap-up-p', 'eff-in',
                                                      'eff-out', 'inv-cost-p', 'inv-cost-c', 'fix-cost-p', 'fix-cost-c', 'var-cost-p', 'var-cost-c', 'wacc', 'depreciation',
                                                      'init', 'discharge', 'ep-ratio', 'c-block', 'p-block', 'decommissionable', 'decom-saving-p', 'decom-saving-c'])

    print("Write Excel File with constructed Sheets")

### Write Excel file with the constructed sheets
    with pd.ExcelWriter(SAVE_PATH, engine='xlsxwriter', options={'strings_to_numbers': True}) as writer:
        global_df.to_excel(writer, sheet_name='Global', index=True)
        print("added global_df")
        site_df.to_excel(writer, sheet_name='Site', index=False)
        print("added site_df")
        commodity_df.to_excel(writer, sheet_name='Commodity', index=False)
        print("added commodity_df")
        process_df.to_excel(writer, sheet_name='Process', index=False)
        print("added process_df")
        process_commodity_df.to_excel(writer, sheet_name='Process-Commodity', index=False)
        print("added process_commodity_df")
        transmission_df.to_excel(writer, sheet_name='Transmission', index=False)
        print("added transmission_df")
        storage_df.to_excel(writer, sheet_name='Storage', index=False)
        print("added storage_df")
        demand_df.to_excel(writer, sheet_name='Demand', index=True)
        print("added demand_df")
        supim_df.to_excel(writer, sheet_name='SupIm', index=True)
        print("added supim_df")
        buysellprice_df.to_excel(writer, sheet_name='Buy-Sell-Price', index=True)
        print("added buysellprice_df")
        timevareff_df.to_excel(writer, sheet_name='TimeVarEff', index=True)
        print("added timevareff_df")

    print("Done")

if __name__ == '__main__': 
    convertPandapower2Urbs()
