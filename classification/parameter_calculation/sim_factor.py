import networkx as nx
import pandas as pd

from pylovo.config_version import SIM_FACTOR
from pylovo.utils import oneSimultaneousLoad


def calculate_line_with_sim_factor(pandapower_net, networkx_graph) -> pd.DataFrame:
    """calculate the sim factor for each line segment"""
    df_sim_factor_definitions = pd.DataFrame.from_dict(SIM_FACTOR, orient='index')
    df_sim_factor_definitions.reset_index(inplace=True)
    df_sim_factor_definitions.columns = ['description', 'sim_factor']

    # The idea is to add to each line a new attributes: these are needed
    # to calculate the simultaneity factor for each line (cable segment).
    # The simultaneity factor is needed to calculate the vsw

    net_line_with_sim_factor = pandapower_net.line
    net_line_with_sim_factor['sim_factor_cumulated'] = ''
    net_line_with_sim_factor['sim_load'] = ''
    net_line_with_sim_factor['no_commercial'] = ''
    net_line_with_sim_factor['load_commercial_mw'] = ''
    net_line_with_sim_factor['no_public'] = ''
    net_line_with_sim_factor['load_public_mw'] = ''
    net_line_with_sim_factor['no_residential'] = ''
    net_line_with_sim_factor['load_residential_mw'] = ''
    net_line_with_sim_factor.drop(['c_nf_per_km', 'g_us_per_km', 'max_i_ka', 'df', 'type', 'in_service'], axis=1,
                                  inplace=True)
    net_line_with_sim_factor = net_line_with_sim_factor.drop_duplicates()

    # First we calculate the sim factor for the consumers/ consumer buses

    level1 = pd.merge(left=pandapower_net.load, left_on='bus', right=pandapower_net.bus,
                      right_on=pandapower_net.bus.index)
    level1.replace(['MFH', 'SFH', 'AB', 'TH'], 'Residential', inplace=True)

    load_value = level1.groupby(['bus', 'zone'])['max_p_mw'].sum()
    load_value = pd.DataFrame(load_value)
    load_value = load_value.reset_index()

    load_count = level1.groupby(['bus', 'zone'])['name_x'].count()
    load_count = pd.DataFrame(load_count)
    load_count = load_count.reset_index()
    load_count = load_count.rename(columns={'name_x': 'count'})

    load_count = pd.merge(left=load_count, left_on='bus', right=load_value, right_on='bus')
    load_count.drop(['zone_y'], axis=1, inplace=True)

    load_count_cat = pd.merge(left=load_count, left_on='zone_x', right=df_sim_factor_definitions,
                              right_on='description')

    load_count_cat = load_count_cat.assign(
        sim_factor_level1=lambda x: oneSimultaneousLoad(installed_power=1, load_count=x['count'],
                                                        sim_factor=x['sim_factor']))

    load_count_cat = load_count_cat.assign(sim_load_level1=lambda x: x['max_p_mw'] * x['sim_factor_level1'])

    # we can now enter these values in our lines table

    for index, row in load_count_cat.iterrows():
        bus = row['bus']
        index_line = net_line_with_sim_factor.index[net_line_with_sim_factor['to_bus'] == bus].tolist()
        net_line_with_sim_factor.at[index_line[0], 'sim_factor_cumulated'] = row['sim_factor_level1']
        net_line_with_sim_factor.at[index_line[0], 'sim_load'] = row['sim_load_level1']
        if row['description'] == 'Commercial':
            net_line_with_sim_factor.at[index_line[0], 'no_commercial'] = row['count']
            net_line_with_sim_factor.at[index_line[0], 'load_commercial_mw'] = row['max_p_mw']
            net_line_with_sim_factor.at[index_line[0], 'no_public'] = 0
            net_line_with_sim_factor.at[index_line[0], 'load_public_mw'] = 0
            net_line_with_sim_factor.at[index_line[0], 'no_residential'] = 0
            net_line_with_sim_factor.at[index_line[0], 'load_residential_mw'] = 0
        elif row['description'] == 'Public':
            net_line_with_sim_factor.at[index_line[0], 'no_commercial'] = 0
            net_line_with_sim_factor.at[index_line[0], 'load_commercial_mw'] = 0
            net_line_with_sim_factor.at[index_line[0], 'no_public'] = row['count']
            net_line_with_sim_factor.at[index_line[0], 'load_public_mw'] = row['max_p_mw']
            net_line_with_sim_factor.at[index_line[0], 'no_residential'] = 0
            net_line_with_sim_factor.at[index_line[0], 'load_residential_mw'] = 0
        elif row['description'] == 'Residential':
            net_line_with_sim_factor.at[index_line[0], 'no_commercial'] = 0
            net_line_with_sim_factor.at[index_line[0], 'load_commercial_mw'] = 0
            net_line_with_sim_factor.at[index_line[0], 'no_public'] = 0
            net_line_with_sim_factor.at[index_line[0], 'load_public_mw'] = 0
            net_line_with_sim_factor.at[index_line[0], 'no_residential'] = row['count']
            net_line_with_sim_factor.at[index_line[0], 'load_residential_mw'] = row['max_p_mw']

    # lets work on the connection nodebuses and their sim factor

    connection_bus = pandapower_net.bus
    connection_bus['connection_bus'] = connection_bus['name'].str.contains("Connection Nodebus")
    connection_bus = connection_bus[connection_bus['connection_bus']]
    connection_bus = connection_bus.index
    connection_bus = list(connection_bus)

    # we sort them by their distance ( number of edges that need to be passed ) along the graph to the trafo.
    df_connection_bus = pd.DataFrame(connection_bus, columns=['bus'])
    df_connection_bus['source'] = 0

    len_path_list = []
    for index, row in df_connection_bus.iterrows():
        length = nx.shortest_path_length(networkx_graph, source=row['source'], target=row['bus'])
        len_path_list.append(length)
    df_connection_bus['len_to_trafo_in_graph'] = len_path_list
    # The connection nodebuses furthest away need to be adressed first.
    df_connection_bus = df_connection_bus.sort_values(by=['len_to_trafo_in_graph'], ascending=False)

    # turn it into a loop
    for index, row in df_connection_bus.iterrows():
        furthest_connection_bus = row['bus']
        connected_downstream = net_line_with_sim_factor[
            net_line_with_sim_factor['from_bus'] == furthest_connection_bus]
        # upstream: towards the trafo
        connected_upstream = net_line_with_sim_factor[net_line_with_sim_factor['to_bus'] == furthest_connection_bus]
        upstream_index = connected_upstream.index
        net_line_with_sim_factor.at[upstream_index[0], 'no_commercial'] = connected_downstream['no_commercial'].sum()
        net_line_with_sim_factor.at[upstream_index[0], 'load_commercial_mw'] = connected_downstream[
            'load_commercial_mw'].sum()
        net_line_with_sim_factor.at[upstream_index[0], 'no_public'] = connected_downstream['no_public'].sum()
        net_line_with_sim_factor.at[upstream_index[0], 'load_public_mw'] = connected_downstream['load_public_mw'].sum()
        net_line_with_sim_factor.at[upstream_index[0], 'no_residential'] = connected_downstream['no_residential'].sum()
        net_line_with_sim_factor.at[upstream_index[0], 'load_residential_mw'] = connected_downstream[
            'load_residential_mw'].sum()

        load_commercial = oneSimultaneousLoad(
            installed_power=net_line_with_sim_factor.at[upstream_index[0], 'load_commercial_mw'],
            load_count=net_line_with_sim_factor.at[upstream_index[0], 'no_commercial'],
            sim_factor=SIM_FACTOR['Commercial'])

        load_public = oneSimultaneousLoad(
            installed_power=net_line_with_sim_factor.at[upstream_index[0], 'load_public_mw'],
            load_count=net_line_with_sim_factor.at[upstream_index[0], 'no_public'], sim_factor=SIM_FACTOR['Public'])

        load_residential = oneSimultaneousLoad(
            installed_power=net_line_with_sim_factor.at[upstream_index[0], 'load_residential_mw'],
            load_count=net_line_with_sim_factor.at[upstream_index[0], 'no_residential'],
            sim_factor=SIM_FACTOR['Residential'])

        net_line_with_sim_factor.at[upstream_index[0], 'sim_load'] = load_commercial + load_public + load_residential

        peak_load_all_consumer_types = net_line_with_sim_factor.at[upstream_index[0], 'load_commercial_mw'] + \
                                       net_line_with_sim_factor.at[upstream_index[0], 'load_public_mw'] + \
                                       net_line_with_sim_factor.at[upstream_index[0], 'load_residential_mw']
        if peak_load_all_consumer_types == 0:
            net_line_with_sim_factor.at[upstream_index[0], 'sim_factor_cumulated'] = 0
            # print('Connection nodebus error')
        else:
            net_line_with_sim_factor.at[upstream_index[0], 'sim_factor_cumulated'] = (net_line_with_sim_factor.at[
                                                                                          upstream_index[
                                                                                              0], 'sim_load'] /
                                                                                      peak_load_all_consumer_types)

    return net_line_with_sim_factor
