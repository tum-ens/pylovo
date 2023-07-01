import pandas as pd

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