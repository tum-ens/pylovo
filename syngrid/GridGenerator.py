import warnings
from pathlib import Path

import pandapower as pp
import plotly
from pandapower.plotting.plotly import vlevel_plotly
from pandapower.plotting.plotly.mapbox_plot import set_mapbox_token

from syngrid import pgReaderWriter as pg, utils
from syngrid.config_data import *
from syngrid.config_version import *


class ResultExistsError(Exception):
    "Raised when the PLZ has already been created."
    pass


class GridGenerator:
    """
    Generates the grid for the given plz area
    """

    def __init__(self, plz=999999, **kwargs):
        if 'geom_shape' in kwargs:
            self.geom_shape = kwargs.get('geom_shape')
            self.plz = str(plz)
            if len(self.plz) != 6:
                raise ValueError(
                    f'{self.plz} is not a valid ID. Your custom PLZ needs to have exactly 6 digits and cannot start with a leading 0')

        else:
            self.plz = str(plz)

        self.pgr = pg.PgReaderWriter()
        self.pgr.insert_version_if_not_exists()
        self.pgr.insert_parameter_tables(consumer_categories=CONSUMER_CATEGORIES)

        self.logger = utils.create_logger(
            name="GridGenerator", log_file=kwargs.get("log_file", "log.txt"), log_level=LOG_LEVEL
        )

    def __del__(self):
        self.pgr.__del__()

    def generate_grid(self):
        self.check_if_results_exist()
        self.cache_and_preprocess_static_objects()
        self.preprocess_ways()
        self.apply_kmeans_clustering()
        self.position_substations()
        self.install_cables()
        self.pgr.save_and_reset_tables(plz=self.plz)

    def cache_and_preprocess_static_objects(self):
        """
        Caches static objects (postcode, buildings, transformers) from raw data tables and
        stores in temporary tables.
        FROM: postcode, res, oth, transformers
        INTO: postcode_result, buildings_tem
        :return:
        """

        self.pgr.copy_postcode_result_table(self.plz)
        self.logger.info(f"working on plz {self.plz}")

        self.pgr.set_residential_buildings_table(self.plz)
        self.pgr.set_other_buildings_table(self.plz)
        self.logger.info("buildings_tem table prepared")
        self.pgr.remove_duplicate_buildings()
        self.logger.info("duplicate buildings removed from buildings_tem")

        self.pgr.set_loadarea_cluster_siedlungstyp(self.plz)
        self.logger.info("hausabstand and siedlungstyp in postcode_result")

        unloadcount = self.pgr.set_building_peak_load()
        self.logger.info(
            f"building peakload calculated in buildings_tem, {unloadcount} unloaded buildings are removed from "
            f"buildings_tem"
        )
        too_large = self.pgr.zero_too_large_consumers()
        self.logger.info(f"{too_large} too large consumers removed from buildings_tem")

        self.pgr.assign_close_buildings()
        self.logger.info("all close buildings assigned and removed from buildings_tem")

        self.pgr.insert_transformers(self.plz)
        self.logger.info("transformers inserted in to the buildings_tem table")
        # self.pgr.count_indoor_transformers() #TODO: check why uncommented
        self.pgr.drop_indoor_transformers()
        self.logger.info("indoor transformers dropped from the buildings_tem table")

    def preprocess_ways(self):
        """
        Cache ways, create network, connect buildings to the ways network
        FROM: ways, buildings_tem
        INTO: ways_tem, buildings_tem, ways_tem_vertices_pgr, ways_tem_
        :return:
        """
        ways_count = self.pgr.set_ways_tem_table(self.plz)
        self.logger.info(f"ways_tem table filled with {ways_count} ways")
        self.pgr.connect_unconnected_ways()
        self.logger.info("ways connection finished in ways_tem")
        self.pgr.draw_building_connection()
        self.logger.info("building connection finished in ways_tem")

        self.pgr.update_ways_cost()
        unconn = self.pgr.set_vertice_id()
        self.logger.debug(f"vertice id set, {unconn} buildings with no vertice id")

    def apply_kmeans_clustering(self):
        """
        Reads ways and vertices from ways_tem, applies k-means clustering
        FROM: ways_tem, buildings_tem
        INTO: ways_tem, vertices_pgr, buildings_tem,
        :return:
        """

        component, vertices = self.pgr.get_connected_component()
        component_ids = np.unique(component)
        if len(component_ids) > 1:
            for i in range(len(component_ids)):
                component_id = component_ids[i]
                related_vertices = vertices[np.argwhere(component == component_id)]
                conn_building_count = self.pgr.count_connected_buildings(related_vertices)
                if conn_building_count <= 1 or conn_building_count is None:
                    self.pgr.delete_ways(related_vertices)
                    self.pgr.delete_transformers(related_vertices)
                    self.logger.debug(
                        "no building component deleted. Useless ways and transformers are deleted from tem tables."
                    )
                elif conn_building_count >= LARGE_COMPONENT_LOWER_BOUND:
                    cluster_count = int(conn_building_count / LARGE_COMPONENT_DIVIDER)
                    self.pgr.update_large_kmeans_cluster(related_vertices, cluster_count)
                    self.logger.debug(f"large component {i} updated")
                else:
                    self.pgr.update_kmeans_cluster(related_vertices)
                    self.logger.debug(f"component {i} updated")
        elif len(component_ids) == 1:
            conn_building_count = self.pgr.count_connected_buildings(vertices)
            if conn_building_count >= LARGE_COMPONENT_LOWER_BOUND:
                cluster_count = int(conn_building_count / LARGE_COMPONENT_DIVIDER)
                self.pgr.update_large_kmeans_cluster(vertices, cluster_count)
                self.logger.debug(f" {cluster_count} component updated")
            else:
                self.pgr.update_kmeans_cluster(vertices)
                self.logger.debug("component updated")
        else:
            warnings.warn(
                "something wrong with connected component, no component could be fetched from ways_tem"
            )

        no_kmean_count = self.pgr.count_no_kmean_buildings()
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
        kcid_length = self.pgr.get_kcid_length()
        for kcounter in range(kcid_length):
            kcid = self.pgr.get_next_unfinished_kcid(self.plz)
            self.logger.debug(f"working on kcid {kcid}")
            # Building clustering
            transformer_list = self.pgr.get_included_transformers(kcid)
            if len(transformer_list) == 0 or transformer_list is None:
                self.pgr.create_building_clusters_for_kcid(self.plz, kcid)
                self.logger.debug(f"kcid{kcid} has no included transformer, clustering finished")
            else:
                self.pgr.assign_transformer_clusters(self.plz, kcid, transformer_list)
                self.logger.debug(
                    f"kcid{kcid} has {len(transformer_list)} transformers, buildings assigned"
                )
                building_count = self.pgr.count_kmean_cluster_consumers(kcid)
                if building_count > 1:
                    self.pgr.create_building_clusters_for_kcid(self.plz, kcid)
                else:
                    self.pgr.delete_isolated_building(self.plz, kcid)
                self.logger.debug("rest building cluster finished")

            bcid_list = self.pgr.get_unfinished_bcids(self.plz, kcid)
            for bcid in bcid_list:
                # Substation positioning
                if bcid >= 0:
                    utils.positionSubstation(self.pgr, self.plz, kcid, bcid)
                    self.logger.debug(f"substation positioning for kcid{kcid}, bcid{bcid} finished")
                    self.pgr.update_s_max(self.plz, kcid, bcid, 1)
                    self.logger.debug("Smax in building_clusters is updated.")

    def install_cables(self):
        """the pandapower network for each cluster (kcid, bcid) is generated and filled with the corresponding
        bus and line elements"""
        cluster_list = self.pgr.get_list_from_plz(self.plz)
        ci_count = 0
        ci_process = 0
        main_street_available_cables = CABLE_COST_DICT.keys()
        for id in cluster_list:
            kcid = id[0]
            bcid = id[1]
            self.logger.debug(f"working on kcid {kcid}, bcid {bcid}")
            # prepare data
            (
                vertices_dict,
                ont_vertice,
                vertices_list,
                buildings_df,
                consumer_df,
                consumer_list,
                connection_nodes,
            ) = self.pgr.prepare_vertices_list(self.plz, kcid, bcid)

            # Power demand variables
            Pd, load_units, load_type = self.pgr.get_consumer_simultaneous_load_dict(
                consumer_list, buildings_df
            )

            # test cable_cost dict
            local_length_dict = {c: 0 for c in CABLE_COST_DICT.keys()}

            # create network
            net = pp.create_empty_network()

            # add std_type:
            self.pgr.create_cable_std_type(net)

            # Add buses and load to network

            # lv mv bus
            self.pgr.create_lvmv_bus(self.plz, kcid, bcid, net)

            # transformer
            self.pgr.create_transformer(self.plz, kcid, bcid, net)

            # connection nodes
            self.pgr.create_connection_bus(connection_nodes, net)

            # consumer nodes and load
            self.pgr.create_consumer_bus_and_load(
                consumer_list, load_units, net, load_type, buildings_df
            )

            # Add lines to network

            # consider connection nodes to install bus lines, starts from the furthest node and
            # forms increasing branches until current limit satisfied
            # each branch line has a 5 * 1e-6 deviation to see clearly in plotly

            branch_deviation = 0
            connection_node_list = connection_nodes
            while True:
                if len(connection_node_list) == 0:
                    self.logger.debug("main street cable installation finished")
                    break
                if len(connection_node_list) == 1:
                    sim_load = utils.simultaneousPeakLoad(
                        buildings_df, consumer_df, connection_node_list
                    )
                    Imax = sim_load / (VN * V_BAND_LOW * np.sqrt(3))  # current in kA

                    local_length_dict = self.pgr.install_consumer_cables(
                        self.plz,
                        kcid,
                        bcid,
                        branch_deviation,
                        connection_node_list,
                        ont_vertice,
                        vertices_dict,
                        Pd,
                        net,
                        CONNECTION_AVAILABLE_CABLES,
                        local_length_dict,
                    )

                    if connection_node_list[0] == ont_vertice:
                        cable, count = self.pgr.find_minimal_available_cable(
                            Imax, net, main_street_available_cables
                        )
                        self.pgr.create_line_ont_to_lv_bus(
                            self.plz, bcid, kcid, connection_node_list[0], branch_deviation, net, cable, count
                        )
                    else:
                        cable, count = self.pgr.find_minimal_available_cable(
                            Imax,
                            net,
                            main_street_available_cables,
                            vertices_dict[connection_nodes[0]],
                        )
                        length = self.pgr.create_line_start_to_lv_bus(
                            self.plz,
                            bcid,
                            kcid,
                            connection_node_list[0],
                            branch_deviation,
                            net,
                            vertices_dict,
                            cable,
                            count,
                            ont_vertice,
                        )
                        local_length_dict[cable] += length

                    self.pgr.deviate_bus_geodata(
                        connection_node_list, branch_deviation, net
                    )
                    self.logger.debug("main street cable installation finished")
                    break

                # start with finding the furthest node
                furthest_node_path_list = self.pgr.find_furthest_node_path_list(
                    connection_node_list, vertices_dict, ont_vertice
                )
                # create max_possible load branch
                branch_node_list, Imax = self.pgr.get_maximum_load_branch(
                    furthest_node_path_list, buildings_df, consumer_df
                )
                local_length_dict = self.pgr.install_consumer_cables(
                    self.plz,
                    bcid,
                    kcid,
                    branch_deviation,
                    branch_node_list,
                    ont_vertice,
                    vertices_dict,
                    Pd,
                    net,
                    CONNECTION_AVAILABLE_CABLES,
                    local_length_dict,
                )
                # for available load branch, select the min size cable
                branch_distance = vertices_dict[branch_node_list[0]]
                cable, count = self.pgr.find_minimal_available_cable(
                    Imax, net, main_street_available_cables, branch_distance
                )
                # install cables for this branch, start from drawing outer parts, leave the transformer<->starting node line to next step
                # important: always draw line start from closer nodes to further nodes, so that the 'line id = line to node' will be unique
                #           bus line for the branch shall be separately drawn through each connection points
                if len(branch_node_list) >= 2:
                    local_length_dict = self.pgr.create_line_node_to_node(
                        self.plz,
                        kcid,
                        bcid,
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
                    self.pgr.create_line_ont_to_lv_bus(
                        self.plz, bcid, kcid, branch_start_node, branch_deviation, net, cable, count
                    )
                else:
                    length = self.pgr.create_line_start_to_lv_bus(
                        self.plz,
                        bcid,
                        kcid,
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
                self.pgr.deviate_bus_geodata(branch_node_list, branch_deviation, net)

                branch_deviation += 1
                continue
            ci_count += 1
            if ci_count >= len(cluster_list) / 10:
                ci_count = 0
                ci_process += 10
                self.logger.info(f"{ci_process} % finished")
            self.logger.info(f"Saved net for kcid {kcid}, bcid {bcid}")
            self.save_net(net, kcid, bcid)

    def analyse_results(self):
        self.logger.info("start basic result analysis")
        self.pgr.analyse_basic_parameters(self.plz)
        self.logger.info("start cable counting")
        self.pgr.analyse_cables(self.plz)
        self.logger.info("start per trafo analysis")
        self.pgr.analyse_per_trafo_parameters(self.plz)
        self.logger.info("result analysis finished")
        self.pgr.conn.commit()

    def plot_trafo_on_map(self, save_plots: bool = False) -> None:
        """trafo types are plotted by their capacity on plotly basemap
        :param save_plots: option to save the plot, defaults to False
        :type save_plots: bool
         """

        net_plot = pp.create_empty_network()
        cluster_list = self.pgr.get_list_from_plz(self.plz)
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
            net_plot, on_map=True, colors_dict=PLOT_COLOR_DICT, projection="epsg:4326"
        )

        if save_plots:
            savepath_folder = Path(
                RESULT_DIR, "figures", f"version_{VERSION_ID}", self.plz
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
        if SAVE_GRID_FOLDER:
            savepath_folder = Path(RESULT_DIR, "grids", f"version_{VERSION_ID}", self.plz)
            savepath_folder.mkdir(parents=True, exist_ok=True)
            filename = f"kcid{kcid}bcid{bcid}.json"
            savepath_file = Path(savepath_folder, filename)
            pp.to_json(net, filename=savepath_file)

        json_string = pp.to_json(net, filename=None)

        self.pgr.save_net(self.plz, kcid, bcid, json_string)

        self.logger.info(f"Grid with kcid:{kcid} bcid:{bcid} is stored. ")

    def check_if_results_exist(self):
        postcode_count = self.pgr.count_postcode_result(self.plz)
        if postcode_count:
            raise ResultExistsError(
                f"The grids for the postcode area {self.plz} is already generated "
                f"for the version {VERSION_ID}."
            )

    def generate_grid_from_geom(self, buildings):
        self.check_if_results_exist()
        self.cache_and_preprocess_static_objects_from_geom(buildings)
        self.preprocess_ways_from_geom()
        self.apply_kmeans_clustering()
        self.position_substations()
        self.install_cables()
        self.pgr.save_and_reset_tables(plz=self.plz)

    def cache_and_preprocess_static_objects_from_geom(self, buildings):
        """
        Caches static objects (postcode, buildings, transformers) from raw data tables and
        stores in temporary tables.
        FROM: postcode, res, oth, transformers
        INTO: postcode_result, buildings_tem
        :return:
        """

        self.pgr.copy_postcode_result_table_with_new_shape(self.plz, self.geom_shape)
        self.logger.info(f"working on plz {self.plz}")

        # self.pgr.set_residential_buildings_table_from_shape_file(self.plz, self.geom_shape)
        # self.pgr.set_other_buildings_table_from_shapefile(self.plz, self.geom_shape)
        self.pgr.set_residential_buildings_table_from_osmid(self.plz, buildings['res'])
        self.pgr.set_other_buildings_table_from_osmid(self.plz, buildings['oth'])
        self.logger.info("buildings_tem table prepared")
        self.pgr.remove_duplicate_buildings()
        self.logger.info("duplicate buildings removed from buildings_tem")

        self.pgr.set_loadarea_cluster_siedlungstyp(self.plz)
        self.logger.info("hausabstand and siedlungstyp in postcode_result")

        unloadcount = self.pgr.set_building_peak_load()
        self.logger.info(
            f"building peakload calculated in buildings_tem, {unloadcount} unloaded buildings are removed from "
            f"buildings_tem"
        )
        too_large = self.pgr.zero_too_large_consumers()
        self.logger.info(f"{too_large} too large consumers removed from buildings_tem")

        self.pgr.assign_close_buildings()
        self.logger.info("all close buildings assigned and removed from buildings_tem")

        self.pgr.insert_transformers(self.plz)
        self.logger.info("transformers inserted in to the buildings_tem table")
        self.pgr.drop_indoor_transformers()
        self.logger.info("indoor transformers dropped from the buildings_tem table")

    def preprocess_ways_from_geom(self):
        """
        Cache ways, create network, connect buildings to the ways network
        FROM: ways, buildings_tem
        INTO: ways_tem, buildings_tem, ways_tem_vertices_pgr, ways_tem_
        :return:
        """
        ways_count = self.pgr.set_ways_tem_table_from_shapefile(self.geom_shape)
        self.logger.info(f"ways_tem table filled with {ways_count} ways")
        self.pgr.connect_unconnected_ways()
        self.logger.info("ways connection finished in ways_tem")
        self.pgr.draw_building_connection()
        self.logger.info("building connection finished in ways_tem")
        self.pgr.update_ways_cost()
        unconn = self.pgr.set_vertice_id()
        self.logger.info(f"vertice id set, {unconn} buildings with no vertice id")
