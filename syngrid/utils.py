import numpy as np


def simultaneousPeakLoad(buildings_df, consumer_cat_df, vertice_ids):
    # Calculates the simultaneous peak load of buildings with given vertice ids
    subset_df = buildings_df[buildings_df['connection_point'].isin(vertice_ids)]
    # print(f"{len(subset_df)} buildings are given.")
    # print(subset_df)
    occurring_categories = (['SFH', 'MFH', 'AB', 'TH'], ['Commercial'], ['Public'], ['Industrial'])

    # Sim loads from each category to dictionary
    category_load_dict = {}
    for cat in occurring_categories:
        # Aggregate total installed power from the category cat
        installed_power = subset_df[subset_df['type'].isin(cat)]["peak_load_in_kw"].values.sum()  # n*P_0
        # building amount from cat
        load_count = subset_df[subset_df['type'].isin(cat)]['houses_per_building'].values.sum()
        if load_count == 0:
            continue

        sim_factor = consumer_cat_df.loc[cat[0]]['sim_factor']  # g_inf

        # Calculate simultaneous load (Kerber.2011) Gl. 3.2 - S. 23
        sim_load = oneSimultaneousLoad(installed_power, load_count, sim_factor)
        category_load_dict[cat[0]] = sim_load

    # print(category_load_dict)
    # Calculate total sim load (Kiefer S. 142)
    total_sim_load = sum(category_load_dict.values())
    # print(f"Total sim load: {total_sim_load}")

    return total_sim_load


def oneSimultaneousLoad(installed_power, load_count, sim_factor):
    # calculation of the simultaneaous load of multiple consumers of the same kind (public, commercial or residential)
    if isinstance(load_count, int):
        if load_count == 0:
            return 0

    sim_load = installed_power * (sim_factor + (1 - sim_factor) * (load_count ** (-3 / 4)))

    return sim_load


def positionSubstation(pgr, plz, kcid, bcid):
    print("positionSubstation", plz, kcid, bcid)
    # Hole die Gebäuden in dem Load Area Cluster
    connection_points = pgr.getBuildingConnectionPointsFromBc(kcid, bcid)
    print("connectionPoints", connection_points)
    if len(connection_points) == 1:
        pgr.upsertSubstationSelection(plz, kcid, bcid, connection_points[0])
        return
    # Distance Matrix von Verbrauchern in einem Load Area Cluster
    localid2vid, dist_mat, vid2localid = pgr.getDistanceMatrixFromBuildingCluster(kcid, bcid)

    # Calculate the sum of distance*load from each vertice to the others
    loads = pgr.generateLoadVector(kcid, bcid)
    print("LOADSHERE", loads)
    total_load_per_vertice = dist_mat.dot(loads)

    # Find the vertice_id of optimal building
    min_localid = np.argmin(total_load_per_vertice)
    ont_connection_id = int(localid2vid[min_localid])

    pgr.upsertSubstationSelection(plz, kcid, bcid, ont_connection_id)
