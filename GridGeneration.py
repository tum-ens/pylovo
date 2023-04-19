import numpy as np

import syngrid.utils as utils
from syngrid import pgReaderWriter as pg
# import pgReaderWriter_Deniz as pg

if __name__ == "__main__":

    pgr = pg.PgReaderWriter()
    
    plz = 81541
    print(f"working on plz {plz}")
    pgr.getPostcodeResultTable(plz)
    
    pgr.setResidentialBuildingsTable(plz)
    pgr.setOtherBuildingsTable(plz)
    print("buildings_tem table prepared")
    pgr.removeDuplicateBuildings()
    print("duplicate buildings removed from buildings_tem")

    pgr.setLoadareaClusterSiedlungstyp(plz)
    print("hausabstand and siedlungstyp  in postcode_result")

    unloadcount = pgr.setBuildingPeakLoad()
    print(f"building peakload calculated in buildings_tem,{unloadcount} unloaded buildings are removed from buildings_tem")
    too_large = pgr.removeTooLargeConsumers()
    print(f"{too_large} too large consumers removed from buildings_tem")

    pgr.assignCloseBuildings()
    print("all close buildings assigned and removed from buildings_tem")

    pgr.insertTransformers(plz)
    print("transformers inserted in to the buildings_tem table")
    pgr.dropIndoorTransformers()
    print("indoor transformers dropped from the buildings_tem table")

    ways_count = pgr.SetWaysTemTable(plz)
    print(f"ways_tem table filled with {ways_count} ways")
    pgr.connectUnconnectedWays()
    print("ways connection finished")
    pgr.drawBuildingConnection()
    print("building connection finished")
    pgr.updateWaysCost()
    unconn = pgr.setVerticeId()
    print(f'vertice id set, {unconn} buildings with no vertice id')

    component, vertices = pgr.getConnectedComponent()
    component_ids = np.unique(component)
    if len(component_ids) > 1:
        for i in range(0,len(component_ids)):
            component_id = component_ids[i]
            related_vertices = vertices[np.argwhere(component == component_id)]
            conn_building_count = pgr.countConnectedBuildings(related_vertices)
            if conn_building_count <= 1 or conn_building_count == None :
                pgr.deleteUselessWays(related_vertices)
                pgr.deleteUselessTransformers(related_vertices)
                print("no building component deleted")
            elif conn_building_count >= 2000:
                cluster_count = int(conn_building_count/1000)
                pgr.updateLargeKmeansCluster(related_vertices, cluster_count)
                print(f"large component {i} updated")
            else:
                pgr.updateKmeansCluster(related_vertices)
                print(f"component {i} updated")
    elif len(component_ids) == 1:
        conn_building_count = pgr.countConnectedBuildings(vertices)
        if conn_building_count >= 2000:
            cluster_count = int(conn_building_count / 1000)
            pgr.updateLargeKmeansCluster(vertices, cluster_count)
            print(f" {cluster_count} component updated")
        else:
            pgr.updateKmeansCluster(vertices)
            print("component updated")
    else:
        print("something wrong with connected component")


    no_kmean_count = pgr.countNoKmeanBuildings()
    if no_kmean_count != 0 and no_kmean_count != None:
        print("Something wrong with k mean clustering")

    kcid_length = pgr.getKcidLength()
    for kcounter in range(kcid_length):
        kcid = pgr.getNextUnfinishedKcid(plz)
        print(f"working on kcid {kcid}")
        # Building clustering
        transformer_list = pgr.getIncludedTransformers(kcid)
        if len(transformer_list) == 0 or transformer_list == None:
            pgr.createBuildingClustersForKcid(plz, kcid)
            print(f"kcid{kcid} has no included transformer, clustering finished")
        else:
            pgr.asignTransformerClusters(plz, kcid, transformer_list)
            print(f"kcid{kcid} has {len(transformer_list)} transformers, buildings asigned")
            building_count = pgr.countKmeanClusterConsumers(kcid)
            if building_count > 1 :
                pgr.createBuildingClustersForKcid(plz, kcid)
            else: 
                pgr.deleteIsolatedBuilding(plz, kcid)
            print("rest building cluster finished")

        bcid_list = pgr.getUnfinishedBcids(plz, kcid)
        for bcid in bcid_list:
            # Substation positioning
            print(f"working on bcid {bcid}")
            if bcid >= 0:
                utils.positionSubstation(pgr, plz, kcid, bcid)
                print(f"substation positioning for kcid{kcid}, bcid{bcid} finished")
                pgr.updateSmax(plz, kcid, bcid, 1)

    pgr.saveInformationAndResetTables()
    pgr.__del__()