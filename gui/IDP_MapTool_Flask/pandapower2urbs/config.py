import os
#print("HELLLOOOOO " + os.path.dirname(os.path.abspath(__file__)))

LOCAL_PATH =  os.path.dirname(os.path.abspath(__file__))

#SAVE_PATH       =  os.path.join(LOCAL_PATH, './urbs_file.xlsx')                            # path to save exported excel file
SAVE_PATH       =  os.path.join(LOCAL_PATH, '../../urbs_optimizer/Input/urbs_file.xlsx')                    # path to save exported excel file

PANDAPOWER_PATH     = os.path.join(LOCAL_PATH, './dataset/_transmission/test.xlsx')  # path which includes pandapower network files
VOLTAGE_LIMITS_PATH = os.path.join(LOCAL_PATH, './dataset/_transmission/voltage_limits.csv')
CABLE_DATA_PATH     = os.path.join(LOCAL_PATH, './dataset/_transmission/cable_data.csv')
TRAFO_DATA_PATH     = os.path.join(LOCAL_PATH, './dataset/_transmission/trafo_data.csv')

BUILDING_LIST_PATH  = os.path.join(LOCAL_PATH, './dataset/_buildings/building_data.csv')    # path of building information file
BUILDING_CONF_PATH  = os.path.join(LOCAL_PATH, './dataset/_buildings/building_conf.csv') # path of selected buildings file

GLOBAL_PROP_PATH    = os.path.join(LOCAL_PATH, './dataset/global/global.csv')

COM_PROP_PATH       = os.path.join(LOCAL_PATH, './dataset/commodity/com_prop.csv')

PRO_CONF_PATH       = os.path.join(LOCAL_PATH, './dataset/process/pro_conf.csv')
PRO_PROP_PATH       = os.path.join(LOCAL_PATH, './dataset/process/pro_prop.csv')
PRO_COM_PROP_PATH   = os.path.join(LOCAL_PATH, './dataset/process/pro_com_prop.csv')

STO_CONF_PATH       = os.path.join(LOCAL_PATH,'./dataset/storage/sto_conf.csv')
STO_PROP_PATH       = os.path.join(LOCAL_PATH, './dataset/storage/sto_prop.csv')
