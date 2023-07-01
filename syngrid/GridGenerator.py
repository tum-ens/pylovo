import os
import warnings
from pathlib import Path

import numpy as np
import pandapower as pp
import matplotlib.pyplot as plt
from pandapower import io_utils
from pandapower.plotting.plotly import vlevel_plotly
from pandapower.plotting.plotly.mapbox_plot import set_mapbox_token
import plotly

from syngrid.config_data import *
import syngrid.config_version as conf_version
from syngrid import pgReaderWriter as pg, utils


class GridGenerator:
    """
    Generates the grid for the given plz area
    """

    def __init__(self, plz, **kwargs):
        if 'geom_shape' in kwargs:
            self.geom_shape = kwargs.get('geom_shape')
            self.plz = str(plz)
            if len(self.plz) != 6:
                raise ValueError(f'{self.plz} is not a valid ID. Your custom PLZ needs to have exactly 6 digits and cannot start with a leading 0')

        else :
            self.plz = str(plz)

        if 'version_id' in kwargs:
            conf_version.VERSION_ID = kwargs.get('version_id')
            print("GRID GENERATOR ID", kwargs.get('version_id'), conf_version.VERSION_ID)
        
        self.pgr = pg.PgReaderWriter(version_id=conf_version.VERSION_ID)
        self.pgr.insert_version_if_not_exists()
        self.pgr.insert_parameter_tables(consumer_categories=conf_version.CONSUMER_CATEGORIES)

    def __del__(self):
        self.pgr.__del__()

    def generate_grid(self):
        self.check_if_results_exist()
        self.cache_and_preprocess_static_objects()
        self.preprocess_ways()
        self.apply_kmeans_clustering()
        self.position_substations()
        self.install_cables()
        self.pgr.saveInformationAndResetTables(plz=self.plz)

    def cache_and_preprocess_static_objects(self):
        """
        Caches static objects (postcode, buildings, transformers) from raw data tables and
        stores in temporary tables.
        FROM: postcode, res, oth, transformers
        INTO: postcode_result, buildings_tem
        :return:
        """

        self.pgr.copyPostcodeResultTable(self.plz)
        print(f"working on plz {self.plz}")

        self.pgr.setResidentialBuildingsTable(self.plz)
        self.pgr.setOtherBuildingsTable(self.plz)
        print("buildings_tem table prepared")
        self.pgr.removeDuplicateBuildings()
        print("duplicate buildings removed from buildings_tem")

        self.pgr.setLoadareaClusterSiedlungstyp(self.plz)
        print("hausabstand and siedlungstyp in postcode_result")

        unloadcount = self.pgr.setBuildingPeakLoad()
        print(
            f"building peakload calculated in buildings_tem, {unloadcount} unloaded buildings are removed from "
            f"buildings_tem"
        )
        too_large = self.pgr.removeTooLargeConsumers()
        print(f"{too_large} too large consumers removed from buildings_tem")

        self.pgr.assignCloseBuildings()
        print("all close buildings assigned and removed from buildings_tem")

        self.pgr.insertTransformers(self.plz)
        print("transformers inserted in to the buildings_tem table")
        self.pgr.dropIndoorTransformers()
        print("indoor transformers dropped from the buildings_tem table")

    def preprocess_ways(self):
        """
        Cache ways, create network, connect buildings to the ways network
        FROM: ways, buildings_tem
        INTO: ways_tem, buildings_tem, ways_tem_vertices_pgr, ways_tem_
        :return:
        """
        ways_count = self.pgr.SetWaysTemTable(self.plz)
        print(f"ways_tem table filled with {ways_count} ways")
        self.pgr.connectUnconnectedWays()
        print("ways connection finished in ways_tem")
        self.pgr.drawBuildingConnection()
        print("building connection finished in ways_tem")
        self.pgr.updateWaysCost()
        unconn = self.pgr.setVerticeId()
        print(f"vertice id set, {unconn} buildings with no vertice id")

    def apply_kmeans_clustering(self):
        """
        Reads ways and vertices from ways_tem, applies k-means clustering
        FROM: ways_tem, buildings_tem
        INTO: ways_tem, vertices_pgr, buildings_tem,
        :return:
        """

        component, vertices = self.pgr.getConnectedComponent()
        component_ids = np.unique(component)
        if len(component_ids) > 1:
            for i in range(len(component_ids)):
                component_id = component_ids[i]
                related_vertices = vertices[np.argwhere(component == component_id)]
                conn_building_count = self.pgr.countConnectedBuildings(related_vertices)
                if conn_building_count <= 1 or conn_building_count is None:
                    self.pgr.deleteUselessWays(related_vertices)
                    self.pgr.deleteUselessTransformers(related_vertices)
                    print(
                        "no building component deleted. Useless ways and transformers are deleted from tem tables."
                    )
                elif conn_building_count >= conf_version.LARGE_COMPONENT_LOWER_BOUND:
                    cluster_count = int(conn_building_count / conf_version.LARGE_COMPONENT_DIVIDER)
                    self.pgr.updateLargeKmeansCluster(related_vertices, cluster_count)
                    print(f"large component {i} updated")
                else:
                    self.pgr.updateKmeansCluster(related_vertices)
                    print(f"component {i} updated")
        elif len(component_ids) == 1:
            conn_building_count = self.pgr.countConnectedBuildings(vertices)
            if conn_building_count >= conf_version.LARGE_COMPONENT_LOWER_BOUND:
                cluster_count = int(conn_building_count / conf_version.LARGE_COMPONENT_DIVIDER)
                self.pgr.updateLargeKmeansCluster(vertices, cluster_count)
                print(f" {cluster_count} component updated")
            else:
                self.pgr.updateKmeansCluster(vertices)
                print("component updated")
        else:
            warnings.warn(
                "something wrong with connected component, no component could be fetched from ways_tem"
            )

        no_kmean_count = self.pgr.countNoKmeanBuildings()
        if no_kmean_count not in [0, None]:
            warnings.warn("Something wrong with k mean clustering")

    def position_substations(self):
        """
        Iterates over k-means clusters and building clusters inside and positions substations for each cluster.
        Considers existing transformers.
        FROM: buildings_tem, building_clusters
        INTO: buildings_tem, building_clusters,
        :return:
        """
        kcid_length = self.pgr.getKcidLength()
        for kcounter in range(kcid_length):
            kcid = self.pgr.getNextUnfinishedKcid(self.plz) #passt
            print(f"working on kcid {kcid}")
            # Building clustering
            transformer_list = self.pgr.getIncludedTransformers(kcid)
            if len(transformer_list) == 0 or transformer_list is None:
                self.pgr.createBuildingClustersForKcid(self.plz, kcid)
                print(f"kcid{kcid} has no included transformer, clustering finished")
            else:
                self.pgr.asignTransformerClusters(self.plz, kcid, transformer_list)
                print(
                    f"kcid{kcid} has {len(transformer_list)} transformers, buildings assigned"
                )
                building_count = self.pgr.countKmeanClusterConsumers(kcid)
                if building_count > 1:
                    self.pgr.createBuildingClustersForKcid(self.plz, kcid)
                else:
                    self.pgr.deleteIsolatedBuilding(self.plz, kcid)
                print("rest building cluster finished")

            bcid_list = self.pgr.getUnfinishedBcids(self.plz, kcid)
            for bcid in bcid_list:
                # Substation positioning
                if bcid >= 0:
                    utils.positionSubstation(self.pgr, self.plz, kcid, bcid)
                    print(f"substation positioning for kcid{kcid}, bcid{bcid} finished")
                    self.pgr.updateSmax(self.plz, kcid, bcid, 1)
                    print("Smax in building_clusters is updated.")

    def install_cables(self):
        cluster_list = self.pgr.getListFromPlz(self.plz)
        ci_count = 0
        ci_process = 0
        main_street_available_cables = conf_version.CABLE_COST_DICT.keys()
        for id in cluster_list:
            kcid = id[0]
            bcid = id[1]
            print(f"working on kcid {kcid}, bcid {bcid}")
            # prepare data
            (
                vertices_dict,
                ont_vertice,
                vertices_list,
                buildings_df,
                consumer_df,
                consumer_list,
                connection_nodes,
            ) = self.pgr.prepareVerticesList(self.plz, kcid, bcid)

            # Power demand variables
            Pd, load_units, load_type = self.pgr.getConsumerSimultaneousLoadDict(
                utils, consumer_list, buildings_df
            )

            # test cable_cost dict
            local_length_dict = {c: 0 for c in conf_version.CABLE_COST_DICT.keys()}

            # create network
            net = pp.create_empty_network()

            # add std_type:
            self.pgr.createCableStdType(net)

            # lv mv bus
            self.pgr.createLVMVbus(self.plz, kcid, bcid, net)

            # transformer
            self.pgr.createTransformer(self.plz, kcid, bcid, net)

            # connection nodes
            self.pgr.createConnectionBus(connection_nodes, net)

            # consumer nodes and load
            self.pgr.createConsumerBusandLoad(
                consumer_list, load_units, net, load_type, buildings_df
            )

            # consider connection nodes to install bus lines, starts from furthest node and forms increasing branches until current limit satisfied
            # each branch line has a 5 * 1e-6 deviation so as to see clearly in plotly

            branch_deviation = 0
            connection_node_list = connection_nodes
            while True:
                if len(connection_node_list) == 0:
                    print("main street cable installation finished")
                    break
                if len(connection_node_list) == 1:
                    sim_load = utils.simultaneousPeakLoad(
                        buildings_df, consumer_df, connection_node_list
                    )
                    Imax = sim_load / (conf_version.VN * conf_version.V_BAND_LOW * np.sqrt(3))  # current in kA

                    local_length_dict = self.pgr.installConsumerCables(
                        branch_deviation,
                        connection_node_list,
                        ont_vertice,
                        vertices_dict,
                        Pd,
                        net,
                        conf_version.CONNECTION_AVAILABLE_CABLES,
                        local_length_dict,
                    )

                    if connection_node_list[0] == ont_vertice:
                        cable, count = self.pgr.findMinimalAvailableCable(
                            Imax, net, main_street_available_cables
                        )
                        self.pgr.createLineOnttoLVbus(
                            connection_node_list[0], branch_deviation, net, cable, count
                        )
                    else:
                        cable, count = self.pgr.findMinimalAvailableCable(
                            Imax,
                            net,
                            main_street_available_cables,
                            vertices_dict[connection_nodes[0]],
                        )
                        length = self.pgr.createLineStarttoLVbus(
                            connection_node_list[0],
                            branch_deviation,
                            net,
                            vertices_dict,
                            cable,
                            count,
                            ont_vertice,
                        )
                        local_length_dict[cable] += length

                    self.pgr.deviateBusGeodata(
                        connection_node_list, branch_deviation, net
                    )
                    print("main street cable installation finished")
                    break

                # start with finding the furthest node
                furthest_node_path_list = self.pgr.findFurthestNodePathList(
                    connection_node_list, vertices_dict, ont_vertice
                )
                # create max_possible load branch
                branch_node_list, Imax = self.pgr.getMaximumLoadBranch(
                    utils, furthest_node_path_list, buildings_df, consumer_df
                )
                local_length_dict = self.pgr.installConsumerCables(
                    branch_deviation,
                    branch_node_list,
                    ont_vertice,
                    vertices_dict,
                    Pd,
                    net,
                    conf_version.CONNECTION_AVAILABLE_CABLES,
                    local_length_dict,
                )
                # for available load branch, select the min size cable
                branch_distance = vertices_dict[branch_node_list[0]]
                cable, count = self.pgr.findMinimalAvailableCable(
                    Imax, net, main_street_available_cables, branch_distance
                )
                # install cables for this branch, start from drawing outer parts, leave the transformer<->starting node line to next step
                # important: always draw line start from closer nodes to further nodes, so that the 'line id = line to node' will be unique
                #           bus line for the branch shall be separately drawn through each connection points
                if len(branch_node_list) >= 2:
                    local_length_dict = self.pgr.createLineNodetoNode(
                        branch_node_list,
                        branch_deviation,
                        vertices_dict,
                        local_length_dict,
                        cable,
                        ont_vertice,
                        count,
                        net,
                    )
                # start node goes directly to transformer along the street
                branch_start_node = branch_node_list[-1]
                if branch_start_node == ont_vertice:
                    self.pgr.createLineOnttoLVbus(
                        branch_start_node, branch_deviation, net, cable, count
                    )
                else:
                    length = self.pgr.createLineStarttoLVbus(
                        branch_start_node,
                        branch_deviation,
                        net,
                        vertices_dict,
                        cable,
                        count,
                        ont_vertice,
                    )
                    local_length_dict[cable] += length
                for vertice in branch_node_list:
                    connection_node_list.remove(vertice)
                self.pgr.deviateBusGeodata(branch_node_list, branch_deviation, net)

                branch_deviation += 1
                print("branch finished")
                continue
            ci_count += 1
            if ci_count >= len(cluster_list) / 10:
                ci_count = 0
                ci_process += 10
                print(f"{ci_process} % finished")

            self.save_net(net, kcid, bcid)

    def analyse_results(self):
        print("start basic result analysis")
        self.pgr.analyse_basic_parameters(self.plz)
        print("start cable counting")
        self.pgr.analyse_cables(self.plz)
        print("start per trafo analysis")
        self.pgr.analyse_per_trafo_parameters(self.plz)
        print("result analysis finished")
        self.pgr.conn.commit()

    def plot_results(
        self,
        plot_trafo=1,
        plot_load_num_per_trafo=1,
        plot_peak_load_per_trafo=1,
        plot_distance_per_trafo=1,
        plot_cable=1,
        save_plots=True,
    ):

        assert {
            plot_trafo,
            plot_load_num_per_trafo,
            plot_peak_load_per_trafo,
            plot_distance_per_trafo,
            plot_cable,
        }.issubset({0, 1})
        assert isinstance(save_plots, bool)

        trafo_dict = self.pgr.read_trafo_dict(self.plz)
        (
            load_num_per_trafo_dict,
            bus_num_per_trafo_dict,
            peak_load_per_trafo_dict,
            max_distance_per_trafo_dict,
            avg_distance_per_trafo_dict,
        ) = self.pgr.read_per_trafo_dict(self.plz)
        cable_dict = self.pgr.read_cable_dict(self.plz)

        try:
            trafo_num = list(trafo_dict.values())
        except:
            print("trafo num dict not well prepared")
            plot_trafo = 0

        try:
            load_num_dict_keys = np.array(
                list(map(float, load_num_per_trafo_dict.keys()))
            )
        except:
            print("load and bus num dict not well prepared")
            plot_load_num_per_trafo = 0
        else:
            keys_order = np.argsort(load_num_dict_keys)

        try:
            peak_load_dict_test = list(peak_load_per_trafo_dict.values())
        except:
            print("peak load dict not well prepared")
            plot_peak_load_per_trafo = 0

        try:
            distance_dict_test = list(max_distance_per_trafo_dict.values())
        except:
            print("peak load dict not well prepared")
            plot_peak_load_per_trafo = 0

        plot_num = sum(
            [
                plot_trafo,
                plot_load_num_per_trafo,
                plot_peak_load_per_trafo,
                plot_distance_per_trafo,
                plot_cable,
            ]
        )
        plot_position = 0

        if plot_trafo:

            plot_position += 1

            trafo_size = list(map(float, trafo_dict.keys()))

            plt.subplot(plot_num, 1, plot_position)
            plt.bar(
                trafo_size,
                height=trafo_num,
                color="black",
                label="Model result",
            )
            plt.xlabel("trafo_size(kVA)", fontsize=14)
            plt.ylabel("trafo_count", fontsize=14)
            plt.title(
                "trafo count analysis for VERSION {}".format(conf_version.VERSION_ID), fontsize=16
            )

        if plot_load_num_per_trafo:

            box_positions = []
            for i in range(2):
                for k in keys_order:
                    box_positions.append(3 * k + i + 1)

            # plot load_num per trafo:
            plot_position += 1
            plt.subplot(plot_num, 1, plot_position)
            plt.boxplot(
                list(load_num_per_trafo_dict.values())
                + list(bus_num_per_trafo_dict.values()),
                labels=list(load_num_per_trafo_dict.keys()) * 2,
                positions=tuple(box_positions),
                vert=True,
                showfliers=False,
                patch_artist=True,
                notch=True,
            )
            plt.title(
                "load and bus number per trafo for VERSION {}".format(conf_version.VERSION_ID),
                fontsize=16,
            )
            plt.ylabel("trafo_size(kVA)", fontsize=14)
            plt.xlabel("load_number", fontsize=14)

        if plot_peak_load_per_trafo:

            box_positions = []
            for k in keys_order:
                box_positions.append(2 * k + 1)

            # plot bus_num per trafo:
            plot_position += 1
            plt.subplot(plot_num, 1, plot_position)
            plt.boxplot(
                list(peak_load_per_trafo_dict.values()),
                labels=list(load_num_per_trafo_dict.keys()),
                positions=tuple(box_positions),
                vert=True,
                showfliers=False,
                patch_artist=True,
                notch=True,
            )
            plt.title(
                "sim peak load per trafo for VERSION {}".format(conf_version.VERSION_ID), fontsize=16
            )
            plt.ylabel("trafo_size(kVA)", fontsize=14)
            plt.xlabel("sim peak load", fontsize=14)

        if plot_distance_per_trafo:
            box_positions = []
            for i in range(2):
                for k in keys_order:
                    box_positions.append(3 * k + i + 1)

            # plot sim_peak_load per trafo:
            plot_position += 1
            plt.subplot(plot_num, 1, plot_position)
            plt.boxplot(
                list(max_distance_per_trafo_dict.values())
                + list(avg_distance_per_trafo_dict.values()),
                labels=list(max_distance_per_trafo_dict.keys()) * 2,
                positions=tuple(box_positions),
                vert=True,
                showfliers=False,
                patch_artist=True,
                notch=True,
            )
            plt.title(
                "max and avg distance per trafo for VERSION {}".format(conf_version.VERSION_ID),
                fontsize=16,
            )
            plt.ylabel("trafo_size(kVA)", fontsize=14)
            plt.xlabel("max / avg distance", fontsize=14)

        if plot_cable:

            cable_length = list(cable_dict.values())
            cable_label = list(cable_dict.keys())
            top_three = np.argsort(cable_length)[:3]
            explode_list = np.zeros(len(cable_length))
            for i in range(3):
                explode_list[top_three[i]] = 0.1

            plot_position += 1
            plt.subplot(plot_num, 1, plot_position)
            plt.pie(
                cable_length, labels=cable_label, explode=explode_list, autopct="%.1f%%"
            )
            plt.title("cable installed for VERSION {}".format(conf_version.VERSION_ID), fontsize=16)

        plt.show()
        if save_plots:
            savepath_folder = Path(
                RESULT_DIR, "figures", f"version_{conf_version.VERSION_ID}", self.plz
            )
            savepath_folder.mkdir(parents=True, exist_ok=True)
            savepath_file = Path(savepath_folder, "result_analysis_figure.png")
            plt.savefig(savepath_file)

    def plot_trafo_on_map(self, save_plots=False):

        net_plot = pp.create_empty_network()
        cluster_list = self.pgr.getListFromPlz(self.plz)
        grid_index = 1
        set_mapbox_token(
            "pk.eyJ1IjoidG9uZ3llMTk5NyIsImEiOiJjbDZ4bWo0aXQwdWdsM2VxbGltMHNzZGUyIn0.TFDYpXsPsvxWPdPRdgCNhg"
        )
        for kcid, bcid in cluster_list:
            net = self.pgr.read_net(self.plz, kcid, bcid)
            for row in net.trafo[["sn_mva", "lv_bus"]].itertuples():
                trafo_size = round(row.sn_mva * 1e3)
                trafo_geom = np.array(net.bus_geodata.loc[row.lv_bus, ["x", "y"]])
                pp.create_bus(
                    net_plot,
                    name="Distribution_grid_"
                    + str(grid_index)
                    + "<br>"
                    + "transformer: "
                    + str(trafo_size)
                    + "_kVA",
                    vn_kv=trafo_size,
                    geodata=trafo_geom,
                    type="b",
                )
                grid_index += 1

        figure = vlevel_plotly(
            net_plot, on_map=True, colors_dict=conf_version.PLOT_COLOR_DICT, projection="epsg:4326"
        )

        if save_plots:
            savepath_folder = Path(
                RESULT_DIR, "figures", f"version_{conf_version.VERSION_ID}", self.plz
            )
            savepath_folder.mkdir(parents=True, exist_ok=True)
            savepath_file = Path(savepath_folder, "trafo_on_map.html")
            plotly.offline.plot(
                figure,
                filename=savepath_file,
            )

    def save_net(self, net, kcid, bcid):
        """
        Save one grid to file and to database
        """
        savepath_folder = Path(RESULT_DIR, "grids", f"version_{conf_version.VERSION_ID}", self.plz)
        savepath_folder.mkdir(parents=True, exist_ok=True)
        filename = f"kcid{kcid}bcid{bcid}.json"
        savepath_file = Path(savepath_folder, filename)
        pp.to_json(net, filename=savepath_file)

        json_string = pp.to_json(net, filename=None)

        self.pgr.save_net(self.plz, kcid, bcid, json_string)

        print(f"Grid {filename} is stored. ")

    def check_if_results_exist(self):
        postcode_count = self.pgr.countPostcodeResult(self.plz)
        if postcode_count:
            raise ValueError(
                f"The grids for the postcode area {self.plz} is already generated "
                f"for the version {conf_version.VERSION_ID}."
            )


    def generate_grid_from_geom(self):
        self.check_if_results_exist() #passt
        self.cache_and_preprocess_static_objects_from_geom() #passt
        self.preprocess_ways_from_geom() #passt
        self.apply_kmeans_clustering() #passt
        self.position_substations() #passt
        self.install_cables() #passt
        self.pgr.saveInformationAndResetTables(plz=self.plz)


    def cache_and_preprocess_static_objects_from_geom(self):
        """
        Caches static objects (postcode, buildings, transformers) from raw data tables and
        stores in temporary tables.
        FROM: postcode, res, oth, transformers
        INTO: postcode_result, buildings_tem
        :return:
        """

        self.pgr.copyPostcodeResultTableWithNewShape(self.plz, self.geom_shape) #passt
        print(f"working on plz {self.plz}")

        self.pgr.setResidentialBuildingsTableFromShapefile(self.plz, self.geom_shape) #passt
        self.pgr.setOtherBuildingsTableFromShapefile(self.plz, self.geom_shape) #passt
        #print(self.pgr.test__getBuildingGeoJSONFromTEM())
        print("buildings_tem table prepared")
        self.pgr.removeDuplicateBuildings() #passt
        print("duplicate buildings removed from buildings_tem")

        self.pgr.setLoadareaClusterSiedlungstyp(self.plz) #passt
        print("hausabstand and siedlungstyp in postcode_result")

        unloadcount = self.pgr.setBuildingPeakLoad() #passt
        print(
            f"building peakload calculated in buildings_tem, {unloadcount} unloaded buildings are removed from "
            f"buildings_tem"
        )
        too_large = self.pgr.removeTooLargeConsumers() #passt
        print(f"{too_large} too large consumers removed from buildings_tem")

        self.pgr.assignCloseBuildings() #passt
        print("all close buildings assigned and removed from buildings_tem")

        self.pgr.insertTransformers(self.plz) #passt
        print("transformers inserted in to the buildings_tem table")
        self.pgr.dropIndoorTransformers() #passt
        print("indoor transformers dropped from the buildings_tem table")

    def preprocess_ways_from_geom(self):
        """
        Cache ways, create network, connect buildings to the ways network
        FROM: ways, buildings_tem
        INTO: ways_tem, buildings_tem, ways_tem_vertices_pgr, ways_tem_
        :return:
        """
        ways_count = self.pgr.SetWaysTemTableFromShapefile(self.geom_shape) #passt
        print(f"ways_tem table filled with {ways_count} ways")
        self.pgr.connectUnconnectedWays() #passt
        print("ways connection finished in ways_tem")
        self.pgr.drawBuildingConnection() #passt
        print("building connection finished in ways_tem")
        self.pgr.updateWaysCost() #passt
        unconn = self.pgr.setVerticeId() #passt
        print(f"vertice id set, {unconn} buildings with no vertice id")