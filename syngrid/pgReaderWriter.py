import os
import re
import json
import psycopg2 as pg
from sqlalchemy import create_engine, text
import numpy as np
import pandas as pd
import pandapower as pp
import time
from scipy.spatial.distance import squareform
from scipy.cluster.hierarchy import linkage, fcluster
import math
from decimal import *
import pandapower.topology as top
import geopandas as gpd

from syngrid.config_data import *
import syngrid.config_version as conf_version
from syngrid import utils


class PgReaderWriter:
    """
    This class is the interface with the database. Functions communicating with the database
    are listed under this class.
    """

    # Konstruktor
    def __init__(self, **kwargs):
        self.conn = pg.connect(
            database=DBNAME, user=USER, password=PASSWORD, host=HOST, port=PORT
        )
        self.cur = self.conn.cursor()
        self.db_path = f"postgresql+psycopg2://{USER}:{PASSWORD}@{HOST}:{PORT}/{DBNAME}"
        self.sqla_engine = create_engine(self.db_path)
        
        print("PGREADERWRITER ID OLD", conf_version.VERSION_ID)
        if 'version_id' in kwargs:
            conf_version.VERSION_ID = kwargs.get('version_id')
            print("PGREADERWRITER ID", kwargs.get('version_id'), conf_version.VERSION_ID)

        print("PgReaderWriter is constructed. ")

    # Dekonstruktor
    def __del__(self):
        self.cur.close()
        self.conn.close()
        print("PgReaderWriter closed.")

    def getConsumerCategories(self):
        """
        Returns: A dataframe with self-defined consumer categories and typical values
        """
        query = """SELECT * FROM public.consumer_categories"""
        cc_df = pd.read_sql_query(query, self.conn)
        cc_df.set_index("definition", drop=False, inplace=True)
        cc_df.sort_index(inplace=True)
        print("Consumer categories fetched.")
        return cc_df

    def getSiedlungstypFromPlz(self, plz) -> int:
        """
        Args:
            plz:
        Returns: Siedlungstyp: 1=Stadt, 2=Dorf, 3=Land
        """
        siedlung_query = """SELECT siedlungstyp FROM public.postcode_result
            WHERE id = %(p)s 
            LIMIT 1; """
        self.cur.execute(siedlung_query, {"p": plz})
        siedlungstyp = self.cur.fetchone()[0]

        return siedlungstyp

    def getTransformatorData(self, siedlungstyp):  # TODO
        """
        Args:
            siedlungstyp: 1=Stadt, 2=Dorf, 3=Land
        Returns: Übliche Transformatorkapazitäten und Kosten abhängig vom Siedlungstyp
        """
        # if siedlungstyp == 1:
        #     anwendungsgebiet_tuple = (1, 2, 3)
        # elif siedlungstyp == 2:
        #     anwendungsgebiet_tuple = (2, 3, 4)
        # elif siedlungstyp == 3:
        #     anwendungsgebiet_tuple = (3, 4, 5)
        # else:
        #     print("Incorrect settlement type number specified.")
        #     return
        anwendungsgebiet_tuple = (1, 2, 3, 4, 5)

        query = """SELECT betriebsmittel.s_max_kva , kosten_eur
            FROM public.betriebsmittel
            WHERE typ = 'Transformer' AND anwendungsgebiet IN %(tuple)s
            ORDER BY s_max_kva;"""

        self.cur.execute(query, {"tuple": anwendungsgebiet_tuple})
        data = self.cur.fetchall()
        capacities = [i[0] for i in data]
        transformer2cost = {i[0]: i[1] for i in data}

        print("Transformer data fetched.")
        return np.array(capacities), transformer2cost

    def getBuildingsFromKc(
        self,
        kcid,
    ) -> pd.DataFrame:
        """
        Args:
            kcid: kmeans_cluster ID
        Returns: A dataframe with all building information
        """
        buildings_query = """SELECT * FROM public.buildings_tem 
                        WHERE connection_point IS NOT NULL
                        AND k_mean_cluster = %(k)s
                        AND in_building_cluster ISNULL;"""
        params = {"k": kcid}

        buildings_df = pd.read_sql_query(buildings_query, con=self.conn, params=params)
        buildings_df.set_index("vertice_id", drop=False, inplace=True)
        buildings_df.sort_index(inplace=True)

        print(
            f"Building data fetched. {len(buildings_df)} buildings from kc={kcid} ..."
        )

        return buildings_df

    def getBuildingsFromBc(self, plz, kcid, bcid) -> pd.DataFrame:

        buildings_query = """SELECT * FROM buildings_tem
                        WHERE type != 'Transformer'
                        AND in_loadarea_cluster = %(p)s
                        AND in_building_cluster = %(b)s
                        AND k_mean_cluster = %(k)s;"""
        params = {"p": plz, "b": bcid, "k": kcid}

        buildings_df = pd.read_sql_query(buildings_query, con=self.conn, params=params)
        buildings_df.set_index("vertice_id", drop=False, inplace=True)
        buildings_df.sort_index(inplace=True)

        print(f"{len(buildings_df)}Building data fetched. ...")

        return buildings_df

    def prepareVerticesList(self, plz, kcid, bcid):
        vertices_dict, ont_vertice = self.getVerticesFromBcid(plz, kcid, bcid)
        vertices_list = list(vertices_dict.keys())

        buildings_df = self.getBuildingsFromBc(plz, kcid, bcid)
        consumer_df = self.getConsumerCategories()
        consumer_list = buildings_df.vertice_id.to_list()

        connection_nodes = [i for i in vertices_list if i not in consumer_list]

        return (
            vertices_dict,
            ont_vertice,
            vertices_list,
            buildings_df,
            consumer_df,
            consumer_list,
            connection_nodes,
        )

    def getConsumerSimultaneousLoadDict(self, utils, consumer_list, buildings_df):
        Pd = {
            consumer: 0 for consumer in consumer_list
        }  # dict of all vertices in bc, 0 as default
        Load_units = {consumer: 0 for consumer in consumer_list}
        Load_type = {consumer: "SFH" for consumer in consumer_list}

        for row in buildings_df.itertuples():
            Load_units[row.vertice_id] = row.houses_per_building
            Load_type[row.vertice_id] = row.type
            gzf = conf_version.CONSUMER_CATEGORIES.loc[
                conf_version.CONSUMER_CATEGORIES.definition == row.type, "sim_factor"
            ].item()

            Pd[row.vertice_id] = utils.oneSimultaneousLoad(
                row.peak_load_in_kw * 1e-3, row.houses_per_building, gzf
            )  # simultaneous load of each building in mW

        return Pd, Load_units, Load_type

    def createCableStdType(self, net):
        pp.create_std_type(
            net,
            {
                "r_ohm_per_km": 1.15,
                "x_ohm_per_km": 0.09,
                "max_i_ka": 0.103,
                "c_nf_per_km": 0,
                "q_mm2": 16,
            },
            name="NYY 4x16 SE",
            element="line",
        )
        pp.create_std_type(
            net,
            {
                "r_ohm_per_km": 0.524,
                "x_ohm_per_km": 0.085,
                "max_i_ka": 0.159,
                "c_nf_per_km": 0,
                "q_mm2": 35,
            },
            name="NYY 4x35 SE",
            element="line",
        )
        pp.create_std_type(
            net,
            {
                "r_ohm_per_km": 0.164,
                "x_ohm_per_km": 0.08,
                "max_i_ka": 0.313,
                "c_nf_per_km": 0,
                "q_mm2": 185,
            },
            name="NAYY 4x185 SE",
            element="line",
        )
        pp.create_std_type(
            net,
            {
                "r_ohm_per_km": 0.32,
                "x_ohm_per_km": 0.082,
                "max_i_ka": 0.215,
                "c_nf_per_km": 0,
                "q_mm2": 95,
            },
            name="NAYY 4x95 SE",
            element="line",
        )
        pp.create_std_type(
            net,
            {
                "r_ohm_per_km": 0.268,
                "x_ohm_per_km": 0.082,
                "max_i_ka": 0.232,
                "c_nf_per_km": 0,
                "q_mm2": 70,
            },
            name="NYY 4x70 SE",
            element="line",
        )
        pp.create_std_type(
            net,
            {
                "r_ohm_per_km": 0.193,
                "x_ohm_per_km": 0.082,
                "max_i_ka": 0.280,
                "c_nf_per_km": 0,
                "q_mm2": 95,
            },
            name="NYY 4x95 SE",
            element="line",
        )
        return None

    def createLVMVbus(self, plz, kcid, bcid, net):
        geodata = self.getOntGeomFromBcid(plz, kcid, bcid)

        pp.create_bus(
            net,
            name="LVbus 1",
            vn_kv=conf_version.VN * 1e-3,
            geodata=geodata,
            max_vm_pu=conf_version.V_BAND_HIGH,
            min_vm_pu=conf_version.V_BAND_LOW,
            type="n",
        )

        # medium voltage external network and mvbus
        mv_data = (float(geodata[0]), float(geodata[1]) + 1.5 * 1e-4)
        mv_bus = pp.create_bus(
            net,
            name="MVbus 1",
            vn_kv=20,
            geodata=mv_data,
            max_vm_pu=conf_version.V_BAND_HIGH,
            min_vm_pu=conf_version.V_BAND_LOW,
            type="n",
        )
        pp.create_ext_grid(net, bus=mv_bus, vm_pu=1, name="External grid")

        return None

    def createTransformer(self, plz, kcid, bcid, net):
        s_max = self.getSMaxFromBcid(plz, kcid, bcid)
        if s_max in (250, 400, 630):
            trafo_name = f"{str(s_max)} transformer"
            trafo_std = f"{str(s_max * 1e-3)} MVA 20/0.4 kV"
            parallel = 1
        elif s_max in (100, 160):
            trafo_name = f"{str(s_max)} transformer"
            trafo_std = "0.25 MVA 20/0.4 kV"
            parallel = 1
        elif s_max in (500, 800):
            trafo_name = f"{str(s_max * 0.5)} transformer"
            trafo_std = f"{str(s_max * 1e-3 * 0.5)} MVA 20/0.4 kV"
            parallel = 2
        else:
            trafo_name = "630 transformer"
            trafo_std = "0.63 MVA 20/0.4 kV"
            parallel = s_max / 630
        trafo_index = pp.create_transformer(
            net,
            pp.get_element_index(net, "bus", "MVbus 1"),
            pp.get_element_index(net, "bus", "LVbus 1"),
            name=trafo_name,
            std_type=trafo_std,
            tap_pos=0,
            parallel=parallel,
        )
        net.trafo.at[trafo_index, "sn_mva"] = s_max * 1e-3
        return None

    def createConnectionBus(self, connection_nodes, net):
        for i in range(len(connection_nodes)):
            node_geodata = self.getNodeGeom(connection_nodes[i])
            pp.create_bus(
                net,
                name=f"Connection Nodebus {connection_nodes[i]}",
                vn_kv=conf_version.VN * 1e-3,
                geodata=node_geodata,
                max_vm_pu=conf_version.V_BAND_HIGH,
                min_vm_pu=conf_version.V_BAND_LOW,
                type="n",
            )

    def createConsumerBusandLoad(self, consumer_list, Lu, net, load_type, building_df):
        for i in range(len(consumer_list)):
            node_geodata = self.getNodeGeom(consumer_list[i])

            ltype = load_type[consumer_list[i]]

            if ltype in ["SFH", "MFH", "AB", "TH"]:
                peak_load = conf_version.CONSUMER_CATEGORIES.loc[
                    conf_version.CONSUMER_CATEGORIES["definition"] == ltype, "peak_load"
                ].values[0]
            else:
                peak_load = building_df[building_df["vertice_id"] == consumer_list[i]][
                    "peak_load_in_kw"
                ].tolist()[0]

            pp.create_bus(
                net=net,
                name=f"Consumer Nodebus {consumer_list[i]}",
                vn_kv=conf_version.VN * 1e-3,
                geodata=node_geodata,
                max_vm_pu=conf_version.V_BAND_HIGH,
                min_vm_pu=conf_version.V_BAND_LOW,
                type="n",
                zone=ltype,
            )
            for j in range(1, Lu[consumer_list[i]] + 1):
                pp.create_load(
                    net=net,
                    bus=pp.get_element_index(
                        net, "bus", f"Consumer Nodebus {consumer_list[i]}"
                    ),
                    p_mw=0,
                    name=f"Load {consumer_list[i]} household {j}",
                    max_p_mw=peak_load * 1e-3,
                )

    def installConsumerCables(
        self,
        branch_deviation,
        branch_node_list,
        ont_vertice,
        vertices_dict,
        Pd,
        net,
        connection_available_cables,
        local_length_dict,
    ):
        # lines
        # first draw house connections from consumer node to corresponding connection node
        consumer_list = self.getVerticesFromConnectionPoints(branch_node_list)
        branch_consumer_list = [n for n in consumer_list if n in vertices_dict.keys()]
        for vertice in branch_consumer_list:
            path_list = self.getPathToBus(vertice, ont_vertice)
            start_vid = path_list[1]
            end_vid = path_list[0]

            geodata = self.getNodeGeom(start_vid)
            start_node_geodata = (
                float(geodata[0]) + 5 * 1e-6 * branch_deviation,
                float(geodata[1]) + 5 * 1e-6 * branch_deviation,
            )

            end_node_geodata = self.getNodeGeom(end_vid)

            line_geodata = [start_node_geodata, end_node_geodata]

            cost_km = (vertices_dict[end_vid] - vertices_dict[start_vid]) * 1e-3

            count = 1
            sim_load = Pd[end_vid]  # power in Watt
            Imax = sim_load * 1e-3 / (conf_version.VN * conf_version.V_BAND_LOW * np.sqrt(3))  # current in kA
            while True:
                line_df = pd.DataFrame.from_dict(net.std_types["line"], orient="index")
                current_available_cables_df = line_df[
                    (line_df["max_i_ka"] >= Imax / count)
                    & (line_df.index.isin(connection_available_cables))
                ]

                if len(current_available_cables_df) == 0:
                    count += 1
                    continue

                current_available_cables_df["cable_impedence"] = np.sqrt(
                    current_available_cables_df["r_ohm_per_km"] ** 2
                    + current_available_cables_df["x_ohm_per_km"] ** 2
                )  # impedence in ohm / km
                if sim_load <= 100:
                    voltage_available_cables_df = current_available_cables_df[
                        current_available_cables_df["cable_impedence"]
                        <= 2 * 1e-3 / (Imax * cost_km / count)
                    ]
                else:
                    voltage_available_cables_df = current_available_cables_df[
                        current_available_cables_df["cable_impedence"]
                        <= 4 * 1e-3 / (Imax * cost_km / count)
                    ]

                if len(voltage_available_cables_df) == 0:
                    count += 1
                    continue
                else:
                    break

            cable = voltage_available_cables_df.sort_values(
                by=["q_mm2"]
            ).index.tolist()[0]
            local_length_dict[cable] += count * cost_km

            pp.create_line(
                net,
                from_bus=pp.get_element_index(
                    net, "bus", f"Connection Nodebus {start_vid}"
                ),
                to_bus=pp.get_element_index(net, "bus", f"Consumer Nodebus {end_vid}"),
                length_km=cost_km,
                std_type=cable,
                name=f"Line to {end_vid}",
                geodata=line_geodata,
                parallel=count,
            )

        return local_length_dict

    def findMinimalAvailableCable(self, Imax, net, cables_list, distance=0):
        count = 1
        while 1:
            line_df = pd.DataFrame.from_dict(net.std_types["line"], orient="index")
            current_available_cables = line_df[
                (line_df.index.isin(cables_list))
                & (line_df["max_i_ka"] >= Imax / count)
            ]
            if len(current_available_cables) == 0:
                count += 1
                continue

            if distance != 0:
                current_available_cables["cable_impedence"] = np.sqrt(
                    current_available_cables["r_ohm_per_km"] ** 2
                    + current_available_cables["x_ohm_per_km"] ** 2
                )  # impedence in ohm / km
                voltage_available_cables = current_available_cables[
                    current_available_cables["cable_impedence"]
                    <= 400 * 0.045 / (Imax * distance / count)
                ]
                if len(voltage_available_cables) == 0:
                    count += 1
                    continue
                else:
                    cable = voltage_available_cables.sort_values(
                        by=["q_mm2"]
                    ).index.tolist()[0]
                    break
            else:
                cable = current_available_cables.sort_values(
                    by=["q_mm2"]
                ).index.tolist()[0]
                break

        return cable, count

    def createLineNodetoNode(
        self,
        branch_node_list,
        branch_deviation,
        vertices_dict,
        local_length_dict,
        cable,
        ont_vertice,
        count,
        net,
    ):
        for i in range(len(branch_node_list) - 1):
            # to get the line geodata, we now need to consider all the nodes in database, not only connection points
            node_path_list = self.getPathToBus(branch_node_list[i], ont_vertice)
            # end at next connection point
            node_path_list = node_path_list[
                : node_path_list.index(branch_node_list[i + 1]) + 1
            ]
            node_path_list.reverse()  # to keep the correct direction

            start_vid = node_path_list[0]
            end_vid = node_path_list[-1]

            line_geodata = []
            for p in node_path_list:
                node_geodata = self.getNodeGeom(p)
                node_geodata = (
                    float(node_geodata[0]) + 5 * 1e-6 * branch_deviation,
                    float(node_geodata[1]) + 5 * 1e-6 * branch_deviation,
                )
                line_geodata.append(node_geodata)

            cost_km = (vertices_dict[end_vid] - vertices_dict[start_vid]) * 1e-3

            local_length_dict[cable] += count * cost_km
            pp.create_line(
                net,
                from_bus=pp.get_element_index(
                    net, "bus", f"Connection Nodebus {start_vid}"
                ),
                to_bus=pp.get_element_index(
                    net, "bus", f"Connection Nodebus {end_vid}"
                ),
                length_km=cost_km,
                std_type=cable,
                name=f"Line to {end_vid}",
                geodata=line_geodata,
                parallel=count,
            )

        return local_length_dict

    def createLineOnttoLVbus(
        self, branch_start_node, branch_deviation, net, cable, count
    ):
        end_vid = branch_start_node
        node_geodata = self.getNodeGeom(end_vid)
        node_geodata = (
            float(node_geodata[0]) + 5 * 1e-6 * branch_deviation,
            float(node_geodata[1]) + 5 * 1e-6 * branch_deviation,
        )
        lvbus_geodata = (
            net.bus_geodata.loc[pp.get_element_index(net, "bus", "LVbus 1"), "x"]
            + 5 * 1e-6 * branch_deviation,
            net.bus_geodata.loc[pp.get_element_index(net, "bus", "LVbus 1"), "y"],
        )
        line_geodata = [lvbus_geodata, node_geodata]

        cost_km = 0
        pp.create_line(
            net,
            from_bus=pp.get_element_index(net, "bus", "LVbus 1"),
            to_bus=pp.get_element_index(net, "bus", f"Connection Nodebus {end_vid}"),
            length_km=cost_km,
            std_type=cable,
            name=f"Line to {end_vid}",
            geodata=line_geodata,
            parallel=count,
        )

    def createLineStarttoLVbus(
        self,
        branch_start_node,
        branch_deviation,
        net,
        vertices_dict,
        cable,
        count,
        ont_vertice,
    ):

        node_path_list = self.getPathToBus(branch_start_node, ont_vertice)

        line_geodata = []
        for p in node_path_list:
            node_geodata = self.getNodeGeom(p)
            node_geodata = (
                float(node_geodata[0]) + 5 * 1e-6 * branch_deviation,
                float(node_geodata[1]) + 5 * 1e-6 * branch_deviation,
            )
            line_geodata.append(node_geodata)
        lvbus_geodata = (
            net.bus_geodata.loc[pp.get_element_index(net, "bus", "LVbus 1"), "x"]
            + 5 * 1e-6 * branch_deviation,
            net.bus_geodata.loc[pp.get_element_index(net, "bus", "LVbus 1"), "y"],
        )
        line_geodata.append(lvbus_geodata)
        line_geodata.reverse()

        cost_km = vertices_dict[branch_start_node] * 1e-3
        length = count * cost_km  # distance in m
        pp.create_line(
            net,
            from_bus=pp.get_element_index(net, "bus", "LVbus 1"),
            to_bus=pp.get_element_index(
                net, "bus", f"Connection Nodebus {branch_start_node}"
            ),
            length_km=cost_km,
            std_type=cable,
            name=f"Line to {branch_start_node}",
            geodata=line_geodata,
            parallel=count,
        )

        return length

    def findFurthestNodePathList(
        self, connection_node_list, vertices_dict, ont_vertice
    ):
        connection_node_dict = {n: vertices_dict[n] for n in connection_node_list}
        furthest_node = max(connection_node_dict, key=connection_node_dict.get)
        # all the connection nodes in the path from transformer to furthest node are considered as potential branch loads
        furthest_node_path_list = self.getPathToBus(furthest_node, ont_vertice)
        furthest_node_path = [
            p for p in furthest_node_path_list if p in connection_node_list
        ]

        return furthest_node_path

    def getMaximumLoadBranch(
        self, utils, furthest_node_path_list, buildings_df, consumer_df
    ):
        # TODO explanation?
        branch_node_list = []
        for node in furthest_node_path_list:
            branch_node_list.append(node)
            sim_load = utils.simultaneousPeakLoad(
                buildings_df, consumer_df, branch_node_list
            )  # sim_peak load in kW
            Imax = sim_load / (conf_version.VN * conf_version.V_BAND_LOW * np.sqrt(3))  # current in kA
            if Imax >= 0.313 and len(branch_node_list) > 1:
                branch_node_list.remove(node)
                break
            elif Imax >= 0.313 and len(branch_node_list) == 1:
                break
        sim_load = utils.simultaneousPeakLoad(
            buildings_df, consumer_df, branch_node_list
        )
        Imax = sim_load / (conf_version.VN * conf_version.V_BAND_LOW * np.sqrt(3))

        return branch_node_list, Imax

    def deviateBusGeodata(self, branch_node_list, branch_deviation, net):
        for node in branch_node_list:
            net.bus_geodata.at[
                pp.get_element_index(net, "bus", f"Connection Nodebus {node}"), "x"
            ] += (5 * 1e-6 * branch_deviation)
            net.bus_geodata.at[
                pp.get_element_index(net, "bus", f"Connection Nodebus {node}"), "y"
            ] += (5 * 1e-6 * branch_deviation)

    def getVerticesFromBcid(self, lcid, kcid, bcid):
        ont_query = """SELECT ont_vertice_id FROM building_clusters 
                    WHERE version_id = %(v)s 
                    AND loadarea_cluster = %(l)s 
                    AND k_mean_cluster = %(k)s
                    AND building_cluster = %(b)s;"""
        self.cur.execute(ont_query, {"v": conf_version.VERSION_ID, "l": lcid, "k": kcid, "b": bcid})
        ont = self.cur.fetchone()[0]

        consumer_query = """SELECT vertice_id FROM buildings_tem
                    WHERE in_loadarea_cluster = %(l)s 
                    AND k_mean_cluster = %(k)s
                    AND in_building_cluster = %(b)s;"""
        self.cur.execute(consumer_query, {"l": lcid, "k": kcid, "b": bcid})
        consumer = [t[0] for t in self.cur.fetchall()]

        connection_query = """SELECT DISTINCT connection_point FROM buildings_tem
                    WHERE in_loadarea_cluster = %(l)s 
                    AND k_mean_cluster = %(k)s
                    AND in_building_cluster = %(b)s;"""
        self.cur.execute(connection_query, {"l": lcid, "k": kcid, "b": bcid})
        connection = [t[0] for t in self.cur.fetchall()]

        vertices_query = """ SELECT DISTINCT node, agg_cost FROM pgr_dijkstra(
                    'SELECT id, source, target, cost, reverse_cost FROM ways_tem', %(o)s, %(c)s, false) ORDER BY agg_cost;"""
        self.cur.execute(vertices_query, {"o": ont, "c": consumer})
        data = self.cur.fetchall()
        vertice_cost_dict = {
            t[0]: t[1] for t in data if t[0] in consumer or t[0] in connection
        }

        return vertice_cost_dict, ont

    def getVerticesFromConnectionPoints(self, connection):
        query = """SELECT vertice_id FROM buildings_tem 
                    WHERE connection_point IN %(c)s
                    AND type != 'Transformer';"""
        self.cur.execute(query, {"c": tuple(connection)})
        data = self.cur.fetchall()
        return [t[0] for t in data]

    def getPathToBus(self, vertice, ont):
        query = """SELECT node FROM pgr_Dijkstra(
                    'SELECT id, source, target, cost, reverse_cost FROM ways_tem', %(v)s, %(o)s, false);"""
        self.cur.execute(query, {"o": ont, "v": vertice})
        data = self.cur.fetchall()
        way_list = [t[0] for t in data]

        return way_list

    def getOntGeomFromBcid(self, lcid, kcid, bcid):
        query = """SELECT ST_X(ST_Transform(geom,4326)), ST_Y(ST_Transform(geom,4326)) FROM transformer_positions
                    WHERE version_id = %(v)s 
                    AND loadarea_cluster = %(l)s 
                    AND k_mean_cluster = %(k)s
                    AND building_cluster = %(b)s;"""
        self.cur.execute(query, {"v": conf_version.VERSION_ID, "l": lcid, "k": kcid, "b": bcid})
        geo = self.cur.fetchone()

        return geo

    def getNodeGeom(self, vid):
        query = """SELECT ST_X(ST_Transform(the_geom,4326)), ST_Y(ST_Transform(the_geom,4326)) 
                    FROM ways_tem_vertices_pgr
                    WHERE id = %(id)s;"""
        self.cur.execute(query, {"id": vid})
        geo = self.cur.fetchone()

        return geo

    def getSMaxFromBcid(self, lcid, kcid, bcid):
        query = """SELECT s_max FROM building_clusters
                    WHERE version_id = %(v)s 
                    AND loadarea_cluster = %(l)s 
                    AND k_mean_cluster = %(k)s
                    AND building_cluster = %(b)s;"""
        self.cur.execute(query, {"v": conf_version.VERSION_ID, "l": lcid, "k": kcid, "b": bcid})
        s_max = self.cur.fetchone()[0]

        return s_max

    def getListFromPlz(self, plz):
        query = """SELECT DISTINCT k_mean_cluster, building_cluster FROM building_clusters 
                    WHERE  version_id = %(v)s AND loadarea_cluster = %(p)s 
                    ORDER BY k_mean_cluster, building_cluster;"""
        self.cur.execute(query, {"p": plz, "v": conf_version.VERSION_ID})
        cluster_list = self.cur.fetchall()

        return cluster_list
    
    def getListFromShapefile(self, plz):
        query = """SELECT DISTINCT k_mean_cluster, building_cluster FROM building_clusters 
                    WHERE  version_id = %(v)s AND loadarea_cluster = %(p)s 
                    ORDER BY k_mean_cluster, building_cluster;"""
        self.cur.execute(query, {"p": plz, "v": conf_version.VERSION_ID})
        cluster_list = self.cur.fetchall()

        return cluster_list

    def getBuildingConnectionPointsFromBc(self, kcid, bcid):
        """
        Args:
            kcid: kmeans_cluster ID
            bcid: building_cluster ID
        Returns: A daframe with all building information
        """
        count_query = """SELECT DISTINCT connection_point
                        FROM public.buildings_tem
                        WHERE vertice_id IS NOT NULL
                            AND in_building_cluster = %(b)s 
                            AND k_mean_cluster = %(k)s;"""
        params = {"b": bcid, "k": kcid}
        self.cur.execute(count_query, params)
        try:
            cp = [t[0] for t in self.cur.fetchall()]
        except:
            cp = []

        return cp

    def getSingleConnectionPointFromBc(self, kcid, bcid):
        count_query = """SELECT connection_point
                                FROM public.buildings_tem AS b
                                WHERE b.vertice_id IS NOT NULL
                                    AND b.in_building_cluster = %(b)s 
                                    AND b.k_mean_cluster = %(k)s
                                    LIMIT 1;"""
        params = {"b": bcid, "k": kcid}
        self.cur.execute(count_query, params)
        conn_id = self.cur.fetchone()[0]

        return conn_id

    def getDistanceMatrixFromKMeanCluster(self, kcid):
        """
        Args:
            kcid: k_mean_cluster ID
        Returns: Die Distanzmatrix der Gebäuden als np.array und das Mapping zwischen vertice_id und lokale ID als dict
        """
        # Creates a distance matrix from the buildings in the loadarea cluster or smaller in the building cluster

        costmatrix_query = """SELECT * FROM pgr_dijkstraCostMatrix(
                            'SELECT id, source, target, cost, reverse_cost FROM public.ways_tem',
                            (SELECT array_agg(DISTINCT b.connection_point) FROM (SELECT * FROM buildings_tem 
                            WHERE k_mean_cluster = %(k)s
                            AND in_building_cluster ISNULL
                            ORDER BY connection_point) AS b),
                            false);"""
        params = {"k": kcid}

        st = time.time()
        cost_df = pd.read_sql_query(
            costmatrix_query,
            con=self.conn,
            params=params,
            dtype={"start_vid": np.int16, "end_vid": np.int16, "agg_cost": np.int16},
        )
        cost_arr = cost_df.to_numpy()
        et = time.time()
        print(f"Elapsed time for SQL to cost_arr: {et - st}")
        # Speichere die echte vertices_ids mit neuen Indexen
        # 0 5346
        # 1 3263
        # 2 3653
        # ...
        localid2vid = dict(enumerate(cost_df["start_vid"].unique()))
        vid2localid = {y: x for x, y in localid2vid.items()}

        # Square distance matrix
        dist_matrix = np.zeros([len(localid2vid), len(localid2vid)])
        st = time.time()
        for i in range(len(cost_df)):
            start_id = vid2localid[cost_arr[i, 0]]
            end_id = vid2localid[cost_arr[i, 1]]
            dist_matrix[start_id][end_id] = cost_arr[i, 2]
        et = time.time()
        print(f"Elapsed time for dist_matrix creation: {et - st}")
        return localid2vid, dist_matrix, vid2localid

    def getDistanceMatrixFromBuildingCluster(self, kcid, bcid):
        """
        Args:
            kcid: k_mean_cluster ID
            bcid: building_cluster ID
        Returns: Die Distanzmatrix der Gebäuden als np.array und das Mapping zwischen vertice_id und lokale ID als dict
        """
        # Creates a distance matrix from the buildings in the loadarea cluster or smaller in the building cluster

        costmatrix_query = """SELECT * FROM pgr_dijkstraCostMatrix(
                            'SELECT id, source, target, cost, reverse_cost FROM public.ways_tem',
                            (SELECT array_agg(DISTINCT b.connection_point) FROM (SELECT * FROM buildings_tem 
                                WHERE k_mean_cluster = %(k)s
                                AND in_building_cluster = %(b)s 
                                ORDER BY connection_point) AS b),
                            false);"""
        params = {"b": bcid, "k": kcid}

        st = time.time()
        cost_df = pd.read_sql_query(
            costmatrix_query,
            con=self.conn,
            params=params,
            dtype={"start_vid": np.int32, "end_vid": np.int32, "agg_cost": np.int16},
        )
        cost_arr = cost_df.to_numpy()
        et = time.time()
        print(f"Elapsed time for SQL to cost_arr: {et - st}")
        # Speichere die echte vertices_ids mit neuen Indexen
        # 0 5346
        # 1 3263
        # 2 3653
        # ...
        localid2vid = dict(enumerate(cost_df["start_vid"].unique()))
        vid2localid = {y: x for x, y in localid2vid.items()}

        # Square distance matrix
        dist_matrix = np.zeros([len(localid2vid), len(localid2vid)])
        st = time.time()
        for i in range(len(cost_df)):
            start_id = vid2localid[cost_arr[i, 0]]
            end_id = vid2localid[cost_arr[i, 1]]
            dist_matrix[start_id][end_id] = cost_arr[i, 2]
        et = time.time()
        print(f"Elapsed time for dist_matrix creation: {et - st}")
        return localid2vid, dist_matrix, vid2localid

    def upsertBuildingCluster(self, lcid, kcid, bcid, vertices, s_max):
        """
        Assign buildings in buildings_tem the bcid and stores the cluster in building_clusters
        Args:
            bcid: building_cluster ID
            lcid: loadarea_cluster ID
            vertices: Liste der vertice_id von gewählten Gebäuden
            s_max: Scheinleistung der vorgesehenen Transformator
        """
        # Insert references to building elements in which cluster they are.
        building_query = """UPDATE public.buildings_tem 
        SET in_building_cluster = %(bc)s 
        WHERE in_loadarea_cluster = %(lc)s 
        AND k_mean_cluster = %(kc)s 
        AND in_building_cluster ISNULL 
        AND connection_point IN %(vid)s 
        AND type != 'Transformer'; """

        params = {
            "v": conf_version.VERSION_ID,
            "lc": lcid,
            "bc": bcid,
            "kc": kcid,
            "vid": tuple(map(int, vertices)),
        }
        self.cur.execute(building_query, params)

        # Insert new clustering
        cluster_query = """INSERT INTO building_clusters (version_id, loadarea_cluster, k_mean_cluster, building_cluster, s_max) 
                VALUES(%(v)s, %(lc)s, %(kc)s, %(bc)s, %(s)s); """

        params = {"v": conf_version.VERSION_ID, "lc": lcid, "bc": bcid, "kc": kcid, "s": int(s_max)}
        self.cur.execute(cluster_query, params)

    def removeTooLargeConsumers(self):
        """
        Sets the load to zero if the peak load is too large
        :return:
        """
        query = """
            UPDATE buildings_tem SET peak_load_in_kw = 0 
            WHERE peak_load_in_kw > 100 AND type IN ('Commercial', 'Public');
            SELECT COUNT(*) FROM buildings_tem WHERE peak_load_in_kw = 0;"""
        self.cur.execute(query)
        too_large = self.cur.fetchone()[0]

        return too_large

    def clearBuildingClustersInKMeanCluster(self, lcid, kcid):
        # Remove old clustering at same loadarea_cluster
        clear_query = """DELETE FROM building_clusters
                WHERE  version_id = %(v)s 
                AND loadarea_cluster = %(lc)s
                AND k_mean_cluster = %(kc)s
                AND building_cluster >= 0; """

        params = {"v": conf_version.VERSION_ID, "lc": lcid, "kc": kcid}
        self.cur.execute(clear_query, params)
        print(
            f"Building clusters with loadarea_cluster = {lcid}, k_mean cluster = {kcid} area cleared."
        )

    def upsertSubstationSelection(self, lcid, kcid, bcid, connection_id):
        """Writes the vertice_id of chosen building as ONT location in the building_cluster table"""

        query = """UPDATE building_clusters SET ont_vertice_id = %(c)s 
                    WHERE version_id = %(v)s AND loadarea_cluster = %(l)s AND k_mean_cluster = %(k)s AND building_cluster = %(b)s; 
                    UPDATE building_clusters SET model_status = 1 
                    WHERE version_id = %(v)s AND loadarea_cluster = %(l)s AND k_mean_cluster = %(k)s AND building_cluster = %(b)s;
                INSERT INTO transformer_positions (version_id, loadarea_cluster, k_mean_cluster, building_cluster, geom, ogc_fid, comment)
                    VALUES (%(v)s, %(l)s, %(k)s, %(b)s, (SELECT the_geom FROM ways_tem_vertices_pgr WHERE id = %(c)s), %(c)s::varchar, 'on_way');"""
        params = {"v": conf_version.VERSION_ID, "c": connection_id, "b": bcid, "k": kcid, "l": lcid}

        self.cur.execute(query, params)

    def countKmeanClusterConsumers(self, kcid):
        query = """SELECT COUNT(DISTINCT vertice_id) FROM buildings_tem WHERE k_mean_cluster = %(k)s AND type != 'Transformer' AND in_building_cluster ISNULL;"""
        self.cur.execute(query, {"k": kcid})
        count = self.cur.fetchone()[0]

        return count

    def updateSmax(self, plz, kcid, bcid, note: int):
        """
        Updates Smax in building_clusters
        :param plz:
        :param kcid:
        :param bcid:
        :param note:
        :return:
        """
        sdl = self.getSiedlungstypFromPlz(plz)
        transformer_capacities, _ = self.getTransformatorData(sdl)

        if note == 0:
            old_query = """SELECT s_max FROM building_clusters
                            WHERE  version_id = %(v)s 
                            AND loadarea_cluster = %(p)s
                            AND k_mean_cluster = %(k)s
                            AND building_cluster = %(b)s;"""
            self.cur.execute(
                old_query, {"v": conf_version.VERSION_ID, "p": plz, "k": kcid, "b": bcid}
            )
            s_max = self.cur.fetchone()[0]

            new_s_max = transformer_capacities[transformer_capacities > s_max][0].item()
            update_query = """UPDATE building_clusters SET s_max = %(n)s
                            WHERE version_id = %(v)s 
                            AND loadarea_cluster = %(p)s
                            AND k_mean_cluster = %(k)s
                            AND building_cluster = %(b)s;"""
            self.cur.execute(
                update_query,
                {"v": conf_version.VERSION_ID, "p": plz, "k": kcid, "b": bcid, "n": new_s_max},
            )
        else:
            double_trans = np.multiply(transformer_capacities[2:4], 2)
            combined = np.concatenate((transformer_capacities, double_trans), axis=None)
            np.sort(combined, axis=None)
            old_query = """SELECT s_max FROM building_clusters
                                        WHERE version_id = %(v)s 
                                        AND loadarea_cluster = %(p)s
                                        AND k_mean_cluster = %(k)s
                                        AND building_cluster = %(b)s;"""
            self.cur.execute(
                old_query, {"v": conf_version.VERSION_ID, "p": plz, "k": kcid, "b": bcid}
            )
            s_max = self.cur.fetchone()[0]
            if s_max in combined.tolist():
                return None
            new_s_max = np.ceil(s_max / 630) * 630
            update_query = """UPDATE building_clusters SET s_max = %(n)s
                                            WHERE version_id = %(v)s 
                                            AND loadarea_cluster = %(p)s 
                                            AND k_mean_cluster = %(k)s 
                                            AND building_cluster = %(b)s;"""
            self.cur.execute(
                update_query,
                {"v": conf_version.VERSION_ID, "p": plz, "k": kcid, "b": bcid, "n": new_s_max},
            )
            print("double or multiple transformer group s_max assigned")

    def setOneBuildingKmeanCluster(self, plz, kcid):
        count_query = """SELECT COUNT(*) FROM buildings_tem WHERE k_mean_cluster = %(k)s AND type = 'Transformer';"""
        self.cur.execute(count_query, {"k": kcid})
        count = self.cur.fetchone()[0]

        if count >= 1:
            query_1 = """
                WITH one (ogc_fid) AS 
                SELECT osm_id FROM buildings_tem b WHERE b.k_mean_cluster = %(k)s AND b.type = 'Transformer' ORDER BY b.center<->
                    (SELECT center FROM buildings_tem WHERE k_mean_cluster = %(k)s AND type != 'Transformer' LIMIT 1) LIMIT 1
                INSERT INTO transformer_positions (version_id, loadarea_cluster, ogc_fid, geom)
                    SELECT %(v)s as version_id, in_loadarea_cluster, osm_id, center FROM buildings_tem AS b, one AS o 
                        WHERE b.k_mean_cluster = %(k)s AND b.type = 'Transformer' AND b.osm_id != o.ogc_fid;
                UPDATE transformer_positions SET comment = 'Unused' WHERE comment ISNULL;
                DELETE FROM buildings_tem WHERE k_mean_cluster = %(k)s AND type = 'Transformer' AND osm_id IN (
                    SELECT ogc_fid FROM transformer_positions WHERE version_id = %(v)s AND loadarea_cluster = %(p)s AND comment = 'Unused');
                UPDATE buildings_tem SET in_building_cluster = -1 WHERE k_mean_cluster = %(k)s;
                INSERT INTO building_clusters (version_id, loadarea_cluster, k_mean_cluster, building_cluster, ont_vertice_id, s_max)
                VALUES (%(v)s, %(p)s, %(k)s,-1, 
                    (SELECT vertice_id FROM buildings_tem WHERE k_mean_cluster = %(k)s AND type = 'Transformer'), 
                    (SELECT CEILING(SUM(peak_load_in_kw)) FROM buildings_tem WHERE k_mean_cluster = %(k)s AND type != 'Transformer'));
                INSERT INTO transformer_positions (version_id, loadarea_cluster,k_mean_cluster, building_cluster, ogc_fid, geom)
                    SELECT %(v)s as version_id, in_loadarea_cluster, k_mean_cluster, in_building_cluster, osm_id, center FROM buildings_tem
                        WHERE k_mean_cluster = %(k)s AND type = 'Transformer';
                UPDATE transformer_positions SET comment = 'Normal' WHERE comment ISNULL;"""
            self.cur.execute(query_1, {"v": conf_version.VERSION_ID, "k": kcid, "p": plz})
        else:
            query_2 = """UPDATE buildings_tem SET in_building_cluster = -1 WHERE k_mean_cluster = %(k)s;
                INSERT INTO transformer_positions (version_id, ogc_fid, loadarea_cluster, k_mean_cluster, building_cluster)
                    SELECT %(v)s as version_id, connection_point::varchar, in_loadarea_cluster, k_mean_cluster, in_building_cluster FROM buildings_tem 
                    WHERE k_mean_cluster = %(k)s LIMIT 1;
                UPDATE transformer_positions t SET geom = (SELECT the_geom FROM ways_tem_vertices_pgr AS w WHERE w.id = t.ogc_fid::int) WHERE geom ISNULL;
                UPDATE transformer_positions SET comment = 'on_way' WHERE comment ISNULL;
                INSERT INTO building_clusters (version_id, loadarea_cluster, k_mean_cluster, building_cluster, ont_vertice_id, s_max)
                    VALUES (%(v)s as version_id, %(p)s, %(k)s, -1,
                    (SELECT connection_point FROM buildings_tem WHERE k_mean_cluster = %(k)s LIMIT 1),
                    (SELECT CEILING(SUM(peak_load_in_kw)) FROM buildings_tem WHERE k_mean_cluster = %(k)s AND type != 'Transformer'));"""
            self.cur.execute(query_2, {"v": conf_version.VERSION_ID, "k": kcid, "p": plz})

    def getUnfinishedBcids(self, lcid, kcid):
        """
        Args:
            lcid: loadarea_cluster ID
        Returns: Eine Liste von nicht modellierten building_cluster ID aus dem gegebenen loadarea
        """
        query = """SELECT DISTINCT building_cluster FROM building_clusters
            WHERE version_id = %(v)s 
            AND k_mean_cluster = %(kc)s
            AND loadarea_cluster = %(lc)s
            AND model_status ISNULL
            ORDER BY building_cluster; """
        params = {"v": conf_version.VERSION_ID, "lc": lcid, "kc": kcid}
        self.cur.execute(query, params)
        bcid_list = [t[0] for t in data] if (data := self.cur.fetchall()) else []
        return bcid_list

    def getWaysFromBc(self, lcid, kcid, bcid, ofc_fid):
        consumer_query = """SELECT vertice_id FROM buildings_tem 
                            WHERE in_loadarea_cluster = %(l)s
                                AND k_mean_cluster = %(k)s
                                AND in_building_cluster = %(b)s
                                AND type != 'Transformer';"""
        self.cur.execute(consumer_query, {"l": lcid, "k": kcid, "b": bcid})
        consumer_list = [t[0] for t in self.cur.fetchall()]

        vertice_query = """SELECT node,edge FROM pgr_Dijkstra(
                    'SELECT id, source, target, cost, reverse_cost FROM ways_tem', %(o)s, %(c)s, false);"""
        self.cur.execute(vertice_query, {"o": ofc_fid, "c": consumer_list})
        data = self.cur.fetchall()
        vertice_list = list({t[0] for t in data})
        edge_list = list({t[1] for t in data})
        edge_list.remove(-1)

        query = """SELECT * FROM ways_tem 
                    WHERE id IN %(e)s;"""
        ways_df = pd.read_sql_query(query, self.conn, params={"e": tuple(edge_list)})
        print(
            f"Ways imported.{len(ways_df)} ways for lc={lcid}, kc={kcid}, bc={bcid} ..."
        )

        return ways_df, vertice_list

    def getOntInfoFromBc(self, lcid, kcid, bcid):

        query = """SELECT ont_vertice_id, s_max
                    FROM building_clusters
                    WHERE version_id = %(v)s 
                    AND k_mean_cluster = %(k)s
                    AND building_cluster = %(b)s
                    AND loadarea_cluster = %(l)s; """
        params = {"v": conf_version.VERSION_ID, "l": lcid, "k": kcid, "b": bcid}
        self.cur.execute(query, params)
        info = self.cur.fetchall()
        if not info:
            print(f"found no ont information for kcid {kcid}, bcid {bcid}")
            return None

        return {"ont_vertice_id": info[0][0], "s_max": info[0][1]}

    def getCables(self, anw):
        query = """SELECT name, max_i_a, r_mohm_per_km, x_mohm_per_km, z_mohm_per_km, kosten_eur FROM betriebsmittel
                    WHERE typ = 'Kabel' AND anwendungsgebiet IN %(a)s ORDER BY max_i_a DESC; """
        cables_df = pd.read_sql_query(query, self.conn, params={"a": anw})
        print(f"{len(cables_df)} different cable types are imported...")
        return cables_df

    def getNextUnfinishedKcid(self, plz):
        """
        Returns: one unmodeled k_mean_cluster ID
        """
        query = """SELECT k_mean_cluster FROM buildings_tem 
                    WHERE k_mean_cluster NOT IN (
                        SELECT DISTINCT k_mean_cluster FROM building_clusters
                        WHERE version_id = %(v)s AND  building_clusters.loadarea_cluster = %(plz)s) AND k_mean_cluster IS NOT NULL
                    ORDER BY k_mean_cluster
                    LIMIT 1;"""
        self.cur.execute(query, {"v": conf_version.VERSION_ID, "plz": plz})
        kcid = self.cur.fetchone()[0]
        return kcid

    def getNextUnfinishedKcidFromShapefile(self, shape):
        """
        Returns: one unmodeled k_mean_cluster ID
        """
        query = """SELECT k_mean_cluster FROM buildings_tem 
                    WHERE k_mean_cluster NOT IN (
                        SELECT DISTINCT k_mean_cluster FROM building_clusters
                        WHERE version_id = %(v)s AND  building_clusters.loadarea_cluster = %(plz)s) AND k_mean_cluster IS NOT NULL
                    ORDER BY k_mean_cluster
                    LIMIT 1;"""
        self.cur.execute(query, {"v": conf_version.VERSION_ID, "shape": shape})
        kcid = self.cur.fetchone()[0]
        return kcid
    
    def getLcidLength(self):
        query = """SELECT COUNT(DISTINCT cluster_id) FROM loadarea
                   WHERE cluster_id <> -1"""
        self.cur.execute(query)
        lcid_length = self.cur.fetchone()[0]
        print(f"selected lcids altogether {lcid_length} ...")
        return lcid_length

    def countNoKmeanBuildings(self):
        """
        Counts relative buildings in buildings_tem, which could not be clustered via k-means
        :return: count
        """
        query = """SELECT COUNT(*) FROM buildings_tem WHERE peak_load_in_kw != 0 AND k_mean_cluster ISNULL;"""
        self.cur.execute(query)
        count = self.cur.fetchone()[0]

        return count

    def getIncludedTransformers(self, kcid):
        """
        Reads the vertice ids of transformers from a given kcid
        :param kcid:
        :return: list
        """
        query = """SELECT vertice_id FROM buildings_tem WHERE k_mean_cluster = %(k)s AND type = 'Transformer';"""
        self.cur.execute(query, {"k": kcid})
        transformers_list = (
            [t[0] for t in data] if (data := self.cur.fetchall()) else []
        )
        return transformers_list

    def getKcidLength(self):
        query = """SELECT COUNT(DISTINCT k_mean_cluster) FROM buildings_tem WHERE k_mean_cluster IS NOT NULL; """
        self.cur.execute(query)
        kcid_length = self.cur.fetchone()[0]
        return kcid_length

    def getConsumerToTransformerDataframe(self, kcid, transformer_list):
        consumer_query = """SELECT DISTINCT connection_point FROM buildings_tem 
                            WHERE k_mean_cluster = %(k)s AND type != 'Transformer';"""
        self.cur.execute(consumer_query, {"k": kcid})
        consumer_list = [t[0] for t in self.cur.fetchall()]

        cost_query = """SELECT * FROM pgr_dijkstraCost(
                'SELECT id, source, target, cost, reverse_cost FROM ways_tem',
                %(cl)s,%(tl)s,
                false);"""
        cost_df = pd.read_sql_query(
            cost_query,
            con=self.conn,
            params={"cl": consumer_list, "tl": transformer_list},
            dtype={"start_vid": np.int16, "end_vid": np.int16, "agg_cost": np.int16},
        )

        return cost_df

    def asignTransformerClusters(self, plz, kcid, transformer_list):
        """Assign buildings to the existing transformers and store it as bcid in buildings_tem"""
        print(f"{len(transformer_list)} transformer found")

        cost_df = self.getConsumerToTransformerDataframe(kcid, transformer_list)
        cost_df = cost_df.drop(cost_df[cost_df["agg_cost"] >= 300].index)
        # for t in cost_df['end_vid']: cost_df_tem = cost_df[cost_df['end_vid'] == t] cost_list =
        # cost_df_tem.sort_values(by = ['agg_cost'])['agg_cost'].values for i in range(len(cost_list) - 1): if
        # cost_list[i+1] - cost_list[i] >= 60: cost_df = cost_df.drop(cost_df[(cost_df['end_vid'] == t) & (cost_df[
        # 'agg_cost'] > cost_list[i])].index) break
        cost_df = cost_df.sort_values(by=["agg_cost"])
        sim_load = 0
        pre_result_dict = {transformer_id: [] for transformer_id in transformer_list}
        full_transformer_list = []
        assigned_consumer_list = []
        for index, row in cost_df.iterrows():
            start_consumer_id = row["start_vid"]
            end_transformer_id = row["end_vid"]
            if start_consumer_id in assigned_consumer_list:
                continue
            if end_transformer_id in full_transformer_list:
                continue
            pre_result_dict[end_transformer_id].append(start_consumer_id.item())
            sim_load = self.calculateSimLoad(pre_result_dict[end_transformer_id])
            if float(sim_load) >= 630:
                # this transformer can not take anymore consumers, delete every dataframe record related
                print(f"transformer {end_transformer_id} capacity exceeded")
                pre_result_dict[end_transformer_id].remove(start_consumer_id.item())
                full_transformer_list.append(end_transformer_id)
                if len(full_transformer_list) != len(transformer_list):
                    continue
                print("all transformer full")
                break
            else:
                # this consumer has been asigned to specific transformer so delete related in dataframe
                assigned_consumer_list.append(start_consumer_id)

        print("transformer selection finished")
        building_cluster_count = 0
        for transformer_id in transformer_list:
            if len(pre_result_dict[transformer_id]) == 0:
                print(f"transformer {transformer_id} has no asigned consumer, deleted")
                self.deleteUselessTransformers([transformer_id])
            else:
                building_cluster_count = building_cluster_count - 1
                sim_load = self.calculateSimLoad(pre_result_dict[transformer_id])
                possible_transformers = np.array([100, 160, 250, 400, 630])
                sim_load = possible_transformers[
                    possible_transformers > float(sim_load)
                ][0].item()
                self.updateBuildingCluster(
                    transformer_id,
                    pre_result_dict[transformer_id],
                    building_cluster_count,
                    kcid,
                    plz,
                    sim_load,
                )
            continue
        print("pre transformer clusters completed")

    def updateBuildingCluster(
        self, transformer_id, conn_id_list, count, kcid, lcid, sim_Load
    ):
        query = """UPDATE buildings_tem SET in_building_cluster = %(count)s WHERE vertice_id = %(t)s;
                UPDATE buildings_tem SET in_building_cluster = %(count)s WHERE connection_point IN %(c)s AND type != 'Transformer';
                INSERT INTO building_clusters (version_id, loadarea_cluster, k_mean_cluster, building_cluster, ont_vertice_id, s_max)
                    VALUES (%(v)s, %(lc)s, %(k)s, %(count)s, %(t)s, %(l)s);
                INSERT INTO transformer_positions (version_id, loadarea_cluster, k_mean_cluster, building_cluster, geom, ogc_fid, comment)
                    VALUES (%(v)s, %(lc)s, %(k)s, %(count)s, 
                    (SELECT center FROM buildings_tem WHERE vertice_id = %(t)s), 
                    (SELECT osm_id FROM buildings_tem WHERE vertice_id = %(t)s), 'Normal' );"""
        self.cur.execute(
            query,
            {
                "v": conf_version.VERSION_ID,
                "count": count,
                "c": tuple(conn_id_list),
                "t": transformer_id,
                "k": kcid,
                "lc": lcid,
                "l": sim_Load,
            },
        )

    def connectUnconnectedWays(self):
        """
        Updates ways_tem
        :return:
        """
        query = """SELECT public.draw_way_connections();"""
        self.cur.execute(query)

    def calculateSimLoad(self, conn_list):
        residential = """WITH residential AS 
        (SELECT b.peak_load_in_kw AS load, b.houses_per_building AS count, c.sim_factor
        FROM buildings_tem AS b
        LEFT JOIN consumer_categories AS c
        ON b.type = c.definition
        WHERE b.connection_point IN %(c)s AND b.type IN ('SFH','MFH','AB','TH')
        )
        SELECT SUM(load), SUM(count), sim_factor FROM residential GROUP BY sim_factor;
        """
        self.cur.execute(residential, {"c": tuple(conn_list)})

        data = self.cur.fetchone()
        if data:
            residential_load = Decimal(data[0])
            residential_count = Decimal(data[1])
            residential_factor = Decimal(data[2])
            residential_sim_load = residential_load * (
                residential_factor
                + (1 - residential_factor) * (residential_count ** Decimal(-3 / 4))
            )
        else:
            residential_sim_load = 0
        # TODO can the following 4 repetitions simplified with a general function?
        commercial = """WITH commercial AS 
                (SELECT b.peak_load_in_kw AS load, b.houses_per_building AS count, c.sim_factor
                FROM buildings_tem AS b
                LEFT JOIN consumer_categories AS c 
                ON c.definition = b.type
                WHERE b.connection_point IN %(c)s AND b.type = 'Commercial'
                )
                SELECT SUM(load), SUM(count), sim_factor FROM commercial GROUP BY sim_factor;
                """
        self.cur.execute(commercial, {"c": tuple(conn_list)})
        data = self.cur.fetchone()
        if data:
            commercial_load = Decimal(data[0])
            commercial_count = Decimal(data[1])
            commercial_factor = Decimal(data[2])
            commercial_sim_load = commercial_load * (
                commercial_factor
                + (1 - commercial_factor) * (commercial_count ** Decimal(-3 / 4))
            )
        else:
            commercial_sim_load = 0

        public = """WITH public AS 
                    (SELECT b.peak_load_in_kw AS load, b.houses_per_building AS count, c.sim_factor 
                    FROM buildings_tem AS b 
                    LEFT JOIN consumer_categories AS c 
                    ON c.definition = b.type
                    WHERE b.connection_point IN %(c)s AND b.type = 'Public')
                    SELECT SUM(load), SUM(count), sim_factor FROM public GROUP BY sim_factor;
                        """
        self.cur.execute(public, {"c": tuple(conn_list)})
        data = self.cur.fetchone()
        if data:
            public_load = Decimal(data[0])
            public_count = Decimal(data[1])
            public_factor = Decimal(data[2])
            public_sim_load = public_load * (
                public_factor + (1 - public_factor) * (public_count ** Decimal(-3 / 4))
            )
        else:
            public_sim_load = 0

        industrial = """WITH industrial AS 
                    (SELECT b.peak_load_in_kw AS load, b.houses_per_building AS count, c.sim_factor FROM buildings_tem AS b
                     LEFT JOIN consumer_categories AS c 
                     ON c.definition = b.type
                     WHERE b.connection_point IN %(c)s AND b.type = 'Industrial')
                     SELECT SUM(load), SUM(count), sim_factor FROM industrial GROUP BY sim_factor;
                                """
        self.cur.execute(industrial, {"c": tuple(conn_list)})
        data = self.cur.fetchone()
        if data:
            industrial_load = Decimal(data[0])
            industrial_count = Decimal(data[1])
            industrial_factor = Decimal(data[2])
            industrial_sim_load = industrial_load * (
                industrial_factor
                + (1 - industrial_factor) * (industrial_count ** Decimal(-3 / 4))
            )
        else:
            industrial_sim_load = 0

        total_sim_load = (
            residential_sim_load
            + commercial_sim_load
            + industrial_sim_load
            + public_sim_load
        )

        return total_sim_load

    def copyPostcodeResultTable(self, plz):
        """
        Copies the given plz entry from postcode to the postcode_result table
        :param plz:
        :return:
        """
        query = """INSERT INTO postcode_result (version_id, id, geom) 
                    SELECT %(v)s as version_id, plz::INT, geom FROM postcode 
                    WHERE plz = %(p)s
                    LIMIT 1 
                    ON CONFLICT (version_id,id) DO NOTHING;"""

        self.cur.execute(query, {"v": conf_version.VERSION_ID, "p": plz})
        print(self.cur.statusmessage)

    def countPostcodeResult(self, plz):
        """
        :param plz:
        :return:
        """
        query = """SELECT COUNT(*) FROM postcode_result
                    WHERE version_id = %(v)s
                    AND id::INT = %(p)s"""
        self.cur.execute(query, {"v": conf_version.VERSION_ID, "p": plz})
        return int(self.cur.fetchone()[0])

    def setLoadareaClusterSiedlungstyp(self, plz):
        """
        Sets hausabstand and siedlungstyp for the given plz in table postcode_result
        :param plz:
        :return:
        """
        distance_query = """WITH some_buildings AS(
                                SELECT osm_id, center FROM buildings_tem
                                ORDER BY RANDOM ()
                                LIMIT 50) 
                            SELECT b.osm_id, d.dist 
                            FROM some_buildings AS b
                            LEFT JOIN LATERAL(
                            SELECT ST_Distance(b.center, b2.center) AS dist
                            FROM buildings_tem AS b2
                            WHERE b.osm_id <> b2.osm_id
                            ORDER BY b.center <-> b2.center
                            LIMIT 4) AS d
                            ON TRUE;"""
        self.cur.execute(distance_query)
        data = self.cur.fetchall()
        if len(data) == 0:
            raise ValueError("There is no building in the buildings_tem table!")
        distance = [t[1] for t in data]
        avg_dis = int(sum(distance) / len(distance))

        query = """ UPDATE postcode_result
                    SET hausabstand = %(avg)s 
                    WHERE version_id = %(v)s 
                    AND id = %(p)s;
                    UPDATE postcode_result
                    SET siedlungstyp = (CASE
                    WHEN hausabstand < 25 THEN 3
                    WHEN 25 <= hausabstand AND hausabstand < 45 THEN 2
                    ELSE 1 END)
                    WHERE version_id = %(v)s 
                    AND id = %(p)s;"""
        self.cur.execute(query, {"v": conf_version.VERSION_ID, "avg": avg_dis, "p": plz})

    def setBuildingPeakLoad(self):
        """
        * Sets the area, type and peak_load in the buildings_tem table
        * Removes buildings with zero load from the buildings_tem table
        :return: Number of removed unloaded buildings from buildings_tem
        """
        query = """
            UPDATE buildings_tem SET area = ST_Area(geom);
            UPDATE buildings_tem SET houses_per_building = (CASE
            WHEN type IN ('TH','Commercial','Public','Industrial') THEN 1
            WHEN type = 'SFH' AND area < 160 THEN 1
            WHEN type = 'SFH' AND area >= 160 THEN 2
            WHEN type IN ('MFH','AB') THEN floor(area/50) * floors
            ELSE 0
            END);
            UPDATE buildings_tem b SET peak_load_in_kw = (CASE
            WHEN b.type IN ('SFH','TH','MFH','AB') THEN b.houses_per_building*(SELECT peak_load FROM consumer_categories WHERE definition = b.type)								  
            WHEN b.type IN ('Commercial','Public','Industrial') THEN b.area*(SELECT peak_load_per_m2 FROM consumer_categories WHERE definition = b.type)/1000 
            ELSE 0
            END);"""
        self.cur.execute(query)

        count_query = (
            """SELECT COUNT(*) FROM buildings_tem WHERE peak_load_in_kw = 0;"""
        )
        self.cur.execute(count_query)
        count = self.cur.fetchone()[0]

        delete_query = """DELETE FROM buildings_tem WHERE peak_load_in_kw = 0;"""
        self.cur.execute(delete_query)

        return count

    def setResidentialBuildingsTable(self, plz):
        """
        * Fills buildings_tem with residential buildings which are inside the plz area
        * Sets the loadarea cluster initially to plz
        :param plz:
        :return:
        """

        # Fill table
        query = """INSERT INTO buildings_tem (osm_id, area, type, geom, center, floors)
                SELECT osm_id, area, building_t, geom, ST_Centroid(geom), floors::int FROM res
                WHERE ST_Contains((SELECT post.geom FROM postcode_result as post WHERE version_id = %(v)s
                AND id = %(plz)s LIMIT 1), ST_Centroid(res.geom));
                UPDATE buildings_tem SET in_loadarea_cluster = %(plz)s WHERE in_loadarea_cluster ISNULL;"""
        self.cur.execute(query, {"v": conf_version.VERSION_ID, "plz": plz})
        print(self.cur.statusmessage)

    def setOtherBuildingsTable(self, plz):
        """
        * Fills buildings_tem with other buildings which are inside the plz area
        * Sets the loadarea cluster initially to plz
        * Sets all floors to 1
        :param plz:
        :return:
        """

        # Fill table
        query = """INSERT INTO buildings_tem(osm_id, area, type, geom, center)
                SELECT osm_id, area, use, geom, ST_Centroid(geom) FROM oth AS o 
                WHERE o.use in ('Commercial', 'Public')
                AND ST_Contains((SELECT post.geom FROM postcode_result as post WHERE version_id = %(v)s
                    AND  id = %(plz)s), ST_Centroid(o.geom));;
            UPDATE buildings_tem SET in_loadarea_cluster = %(plz)s WHERE in_loadarea_cluster ISNULL;
            UPDATE buildings_tem SET floors = 1 WHERE floors ISNULL;"""
        self.cur.execute(query, {"v": conf_version.VERSION_ID, "plz": plz})
        print(self.cur.statusmessage)

    def setResidentialBuildingsTableFromShapefile(self, plz, shape, **kwargs):
        """
        * Fills buildings_tem with residential buildings which are inside the selected polygon
        * Sets the loadarea cluster to first plz that intersects
        :param shape:
        :return:
        """

        # Fill table
        query = """INSERT INTO buildings_tem (osm_id, area, type, geom, center, floors)
                SELECT osm_id, area, building_t, geom, ST_Centroid(geom), floors::int FROM res
                WHERE ST_Contains(ST_Transform(ST_GeomFromGeoJSON(%(shape)s), 3035), ST_Centroid(res.geom));
                UPDATE buildings_tem SET in_loadarea_cluster = %(p)s WHERE in_loadarea_cluster ISNULL;"""
        self.cur.execute(query, {"v": conf_version.VERSION_ID, "p": plz, "shape": shape})
        print(self.cur.statusmessage)

    def setOtherBuildingsTableFromShapefile(self, plz, shape, **kwargs):
        """
        * Fills buildings_tem with other buildings which are inside the selected polygon
        * Sets the loadarea cluster to first plz that intersects shapefile
        * Sets all floors to 1
        :param shape:
        :return:
        """

        # Fill table
        query = """INSERT INTO buildings_tem(osm_id, area, type, geom, center)
                SELECT osm_id, area, use, geom, ST_Centroid(geom) FROM oth AS o 
                WHERE o.use in ('Commercial', 'Public')
                AND ST_Contains(ST_Transform(ST_GeomFromGeoJSON(%(shape)s), 3035), ST_Centroid(o.geom));
            UPDATE buildings_tem SET in_loadarea_cluster = %(p)s WHERE in_loadarea_cluster ISNULL;
            UPDATE buildings_tem SET floors = 1 WHERE floors ISNULL;"""
        self.cur.execute(query, {"v": conf_version.VERSION_ID, "p": plz, "shape": shape})
        print(self.cur.statusmessage)

    def setResidentialBuildingsTableFromOSMID(self, plz, buildings, **kwargs):
        """
        * Fills buildings_tem with residential buildings which are inside the selected polygon
        * Sets the loadarea cluster to first plz that intersects 
        :param shape:
        :return:
        """

        # Fill table
        query = """INSERT INTO buildings_tem (osm_id, area, type, geom, center, floors)
                SELECT osm_id, area, building_t, geom, ST_Centroid(geom), floors::int FROM res
                WHERE res.osm_id = ANY(%(buildings)s);
                UPDATE buildings_tem SET in_loadarea_cluster = %(p)s WHERE in_loadarea_cluster ISNULL;"""
        self.cur.execute(query, {"v": conf_version.VERSION_ID, "p": plz, "buildings": buildings})
        print(self.cur.statusmessage)

    def setOtherBuildingsTableFromOSMID(self, plz, buildings, **kwargs):
        """
        * Fills buildings_tem with other buildings which are inside the selected polygon
        * Sets the loadarea cluster to first plz that intersects shapefile
        * Sets all floors to 1
        :param shape:
        :return:
        """

        # Fill table
        query = """INSERT INTO buildings_tem(osm_id, area, type, geom, center)
                SELECT osm_id, area, use, geom, ST_Centroid(geom) FROM oth AS o 
                WHERE o.use in ('Commercial', 'Public')
                AND o.osm_id = ANY(%(buildings)s);
            UPDATE buildings_tem SET in_loadarea_cluster = %(p)s WHERE in_loadarea_cluster ISNULL;
            UPDATE buildings_tem SET floors = 1 WHERE floors ISNULL;"""
        self.cur.execute(query, {"v": conf_version.VERSION_ID, "p": plz, "buildings": buildings})
        print(self.cur.statusmessage)

    def getConnectedComponent(self):
        """
        Reads from ways_tem
        :return:
        """
        component_query = """SELECT component,node FROM pgr_connectedComponents(
                'SELECT id, source, target, cost, reverse_cost FROM ways_tem');"""
        self.cur.execute(component_query)
        data = self.cur.fetchall()
        component = np.asarray([i[0] for i in data])
        node = np.asarray([i[1] for i in data])

        return component, node

    def countBuildingsinKmeancluster(self, kcid):
        query = """SELECT COUNT(*) FROM buildings_tem WHERE k_mean_cluster = %(k)s;"""
        self.cur.execute(query, {"k": kcid})
        count = self.cur.fetchone()[0]

        return count

    def SetWaysTemTable(self, plz):
        """
        * Inserts ways inside the plz area to the ways_tem table
        :param plz:
        :return: number of ways in ways_tem
        """
        query = """INSERT INTO ways_tem
            SELECT * FROM ways AS w 
            WHERE ST_Intersects(w.geom,(SELECT geom FROM postcode_result WHERE version_id = %(v)s
                    AND  id = %(p)s));
            SELECT COUNT(*) FROM ways_tem;"""
        self.cur.execute(query, {"v": conf_version.VERSION_ID, "p": plz})
        count = self.cur.fetchone()[0]

        if count == 0:
            raise ValueError(f"Ways table is empty for the given plz: {plz}")

        return count

    def SetWaysTemTableFromShapefile(self, shape):
        """
        * Inserts ways inside the plz area to the ways_tem table
        :param plz:
        :return: number of ways in ways_tem
        """
        query = """INSERT INTO ways_tem
            SELECT * FROM ways AS w 
            WHERE ST_Intersects(w.geom, ST_Transform(ST_GeomFromGeoJSON(%(shape)s), 3035));
            SELECT COUNT(*) FROM ways_tem;"""
        self.cur.execute(query, {"v": conf_version.VERSION_ID, "shape": shape})
        count = self.cur.fetchone()[0]

        if count == 0:
            raise ValueError(f"Ways table is empty for the given plz: {shape}")

        return count
    
    def tryClustering(
        self,
        Z,
        cluster_amount,
        localid2vid,
        buildings,
        consumer_cat_df,
        transformer_capacities,
        double_trans,
    ):
        flat_groups = fcluster(Z, t=cluster_amount, criterion="maxclust")
        cluster_ids = np.unique(flat_groups)
        cluster_count = len(cluster_ids)
        # Check if simultaneous load can be satisfied with possible transformers
        cluster_dict = {}
        invalid_cluster_dict = {}
        for cluster_id in range(1, cluster_count + 1):
            vid_list = [
                localid2vid[lid[0]] for lid in np.argwhere(flat_groups == cluster_id)
            ]
            total_sim_load = utils.simultaneousPeakLoad(
                buildings, consumer_cat_df, vid_list
            )
            if (
                total_sim_load >= max(transformer_capacities) and len(vid_list) >= 5
            ):  # the cluster is too big
                invalid_cluster_dict[cluster_id] = vid_list
            elif total_sim_load < max(transformer_capacities):
                # find the smallest transformer, that satisfies the load
                opt_transformer = transformer_capacities[
                    transformer_capacities > total_sim_load
                ][0]
                opt_double_transformer = double_trans[
                    double_trans > total_sim_load * 1.15
                ][0]
                if (opt_double_transformer - total_sim_load) > (
                    opt_transformer - total_sim_load
                ):
                    cluster_dict[cluster_id] = (vid_list, opt_transformer)
                else:
                    cluster_dict[cluster_id] = (vid_list, opt_double_transformer)
            else:
                opt_transformer = math.ceil(total_sim_load)
                cluster_dict[cluster_id] = (vid_list, opt_transformer)
        return invalid_cluster_dict, cluster_dict, cluster_count

    def createBuildingClustersForKcid(self, plz, kcid):
        """
        Cluster buildings with average linkage clustering
        :param plz:
        :param kcid:
        :return:
        """
        # Hole die Gebäuden in dem Load Area Cluster
        buildings = self.getBuildingsFromKc(kcid)
        # Hole die Verbraucherkategorien
        consumer_cat_df = self.getConsumerCategories()
        # Siedlungstyp (1,2,3)
        siedlungstyp = self.getSiedlungstypFromPlz(plz)
        # Transformatordaten
        transformer_capacities, _ = self.getTransformatorData(siedlungstyp)
        double_trans = np.multiply(transformer_capacities[2:4], 2)

        # Distance Matrix von Verbrauchern in einem Load Area Cluster
        localid2vid, dist_mat, vid2localid = self.getDistanceMatrixFromKMeanCluster(
            kcid
        )
        # Transform to a condensed distance vector for linkage
        dist_vector = squareform(dist_mat)
        # hierarchical clustering
        Z = linkage(dist_vector, method="average")

        # transform to flat clustering
        valid_cluster_dict = {}
        invalid_trans_cluster_dict = {}
        cluster_amount = 2

        new_localid2vid = localid2vid
        while True:
            invalid_cluster_dict, cluster_dict, cluster_count = self.tryClustering(
                Z,
                cluster_amount,
                new_localid2vid,
                buildings,
                consumer_cat_df,
                transformer_capacities,
                double_trans,
            )
            # combination and re_index
            if len(cluster_dict) != 0:
                current_valid_amount = len(valid_cluster_dict)
                valid_cluster_dict.update(
                    {x + current_valid_amount: y for x, y in cluster_dict.items()}
                )
                valid_cluster_dict = dict(enumerate(valid_cluster_dict.values()))

            if len(invalid_cluster_dict) != 0:
                current_invalid_amount = len(invalid_trans_cluster_dict)
                invalid_trans_cluster_dict.update(
                    {
                        x + current_invalid_amount: y
                        for x, y in invalid_cluster_dict.items()
                    }
                )
                invalid_trans_cluster_dict = dict(
                    enumerate(invalid_trans_cluster_dict.values())
                )

            if len(invalid_trans_cluster_dict) == 0:
                # terminate when there is not too_large cluster and amount of double transformers are within limit
                # combination and re_index
                print(
                    f"altogether {len(valid_cluster_dict)} single transformer clusters found"
                )
                break

            else:
                print(f"found {len(invalid_trans_cluster_dict)} too_large clusters")
                # first deal with those too_large clusters

                # get local_ids for those buildings which are in a too_large clusters
                # print(f'altogether {len(invalid_cluster_dict)} too_large clusters found')
                invalid_vertice_ids = list(invalid_trans_cluster_dict[0])
                invalid_local_ids = [vid2localid[v] for v in invalid_vertice_ids]
                # print(invalid_local_ids)

                new_localid2vid = {
                    k: v for k, v in localid2vid.items() if k in invalid_local_ids
                }
                new_localid2vid = dict(enumerate(new_localid2vid.values()))

                new_dist_mat = dist_mat[invalid_local_ids]
                new_dist_mat = new_dist_mat[:, invalid_local_ids]
                new_dist_vector = squareform(new_dist_mat)
                Z = linkage(new_dist_vector, method="average")
                cluster_amount = 2
                # have to refresh double_dict every iteration
                del invalid_trans_cluster_dict[0]
                invalid_trans_cluster_dict = dict(
                    enumerate(invalid_trans_cluster_dict.values())
                )

                continue

        # at break of this iteration we have a possible clustering which is electrically valid and of min cluster
        # numbers combine to an overall dict of all buildings about their post_defined cluster_ids:(vid_list,
        # opt_transformer) current_valid_amount = len(valid_cluster_dict) total cost of transformers
        # total_transformer_cost = sum([transformer2cost[v[1]] for v in valid_cluster_dict.values()])

        # record result
        # trafo_count = {100: 0, 160: 0, 250: 0, 400: 0, 630: 0, 1030: 0, 1260: 0}
        # for key in valid_cluster_dict:
        #     trafo_count[valid_cluster_dict[key][1]] = trafo_count[valid_cluster_dict[key][1]] + 1
        # print(trafo_count)

        # Upsert into the database
        self.clearBuildingClustersInKMeanCluster(plz, kcid)
        for bcid in valid_cluster_dict:
            self.upsertBuildingCluster(
                plz,
                kcid,
                bcid,
                vertices=valid_cluster_dict[bcid][0],
                s_max=valid_cluster_dict[bcid][1],
            )
        print(f"BC upsert done for load_cluster {plz} k_mean cluster {kcid} ...")

    def drawBuildingConnection(self):
        """
        Updates ways_tem, creates pgr network topology in new tables: #TODO
        :return:
        """
        connection_query = """ SELECT public.draw_home_connections(); """
        self.cur.execute(connection_query)

        topology_query = """select pgr_createTopology('ways_tem', 0.01, the_geom:='geom', clean:=true) """
        self.cur.execute(topology_query)

        # add_buildings_query = '''SELECT public.add_buildings();'''
        # self.cur.execute(add_buildings_query)
        # self.conn.commit()

        analyze_query = (
            """SELECT pgr_analyzeGraph('ways_tem',0.01, the_geom:='geom'); """
        )
        self.cur.execute(analyze_query)

    def insertTransformers(self, plz):
        """
        * Add up the existing transformers from transformers table to the buildings_tem table
        * Removes all other transformers from the transformers table? #TODO dont remove transformers, just copy relevant ones to the buildings_tem
        :param plz:
        :return:
        """
        insert_query = """
            DELETE FROM transformers WHERE ST_Within(geom, (SELECT geom FROM postcode_result LIMIT 1)) IS FALSE;
            DELETE FROM transformers WHERE voltage IS NOT NULL;
            UPDATE transformers SET geom = ST_Centroid(geom) WHERE ST_GeometryType(geom) =  'ST_Polygon';
            INSERT INTO buildings_tem (osm_id, center)
                SELECT ogc_fid::varchar, geom FROM transformers;
            UPDATE buildings_tem SET in_loadarea_cluster = %(p)s WHERE in_loadarea_cluster ISNULL;
            UPDATE buildings_tem SET type = 'Transformer' WHERE type ISNULL;
            UPDATE buildings_tem SET peak_load_in_kw = -1 WHERE peak_load_in_kw ISNULL;"""
        self.cur.execute(insert_query, {"p": plz})

    def setVerticeId(self):
        """
        Updates buildings_tem with the vertice_id s from ways_tem_vertices_pgr
        :return:
        """
        query = """UPDATE public.buildings_tem b
                SET vertice_id = (SELECT id FROM ways_tem_vertices_pgr AS v 
                WHERE ST_Equals(v.the_geom,b.center));"""
        self.cur.execute(query)

        query2 = """UPDATE buildings_tem b
                SET connection_point = (SELECT target FROM ways_tem WHERE source = b.vertice_id LIMIT 1)
                WHERE vertice_id IS NOT NULL AND connection_point IS NULL;"""
        self.cur.execute(query2)

        count_query = """ SELECT COUNT(*) FROM buildings_tem
            WHERE connection_point IS NULL AND peak_load_in_kw != 0;"""
        self.cur.execute(count_query)
        count = self.cur.fetchone()[0]

        delete_query = """DELETE FROM buildings_tem WHERE connection_point IS NULL AND peak_load_in_kw != 0;"""
        self.cur.execute(delete_query)

        return count

    def generateLoadVector(self, kcid, bcid):
        query = """SELECT SUM(peak_load_in_kw)::float FROM buildings_tem 
                WHERE k_mean_cluster = %(k)s AND in_building_cluster = %(b)s 
                GROUP BY connection_point 
                ORDER BY connection_point;"""
        self.cur.execute(query, {"k": kcid, "b": bcid})
        load = np.asarray([i[0] for i in self.cur.fetchall()])

        return load

    def assignCloseBuildings(self):
        """
        * Set peak load to zero, if a building is too near or touching to a too large customer?
        :return:
        """
        while True:
            remove_query = """WITH close (un) AS (
                    SELECT ST_Union(geom) FROM buildings_tem WHERE peak_load_in_kw = 0)
                    UPDATE buildings_tem b SET peak_load_in_kw = 0 FROM close AS c WHERE ST_Touches(b.geom, c.un) 
                        AND b.type IN ('Commercial', 'Public', 'Industrial')
                        AND b.peak_load_in_kw != 0;"""
            self.cur.execute(remove_query)

            count_query = """WITH close (un) AS (
                    SELECT ST_Union(geom) FROM buildings_tem WHERE peak_load_in_kw = 0)
                    SELECT COUNT(*) FROM buildings_tem AS b, close AS c WHERE ST_Touches(b.geom, c.un) 
                        AND b.type IN ('Commercial', 'Public', 'Industrial')
                        AND b.peak_load_in_kw != 0;"""
            self.cur.execute(count_query)
            count = self.cur.fetchone()[0]
            if count == 0 or count is None:
                break

        return None

    def countConnectedBuildings(self, vertices):
        """
        FROM: buildings_tem
        :param vertices: np.array
        :return: count of buildings with given vertice_id s from buildings_tem
        """
        query = """SELECT COUNT(*) FROM buildings_tem WHERE vertice_id IN %(v)s AND type != 'Transformer';"""
        self.cur.execute(query, {"v": tuple(map(int, vertices))})
        count = self.cur.fetchone()[0]

        return count

    def updateWaysCost(self):
        """
        Calculates the length of each way and stores in ways_tem.cost as meter
        """
        query = """UPDATE ways_tem SET cost = ST_Length(geom); 
        UPDATE ways_tem SET reverse_cost = cost;"""
        self.cur.execute(query)

    def countOneBuildingCluster(self):
        query = """SELECT COUNT(*) FROM building_clusters bc 
            WHERE (SELECT COUNT(*) FROM buildings_tem b WHERE b.k_mean_cluster = bc.k_mean_cluster AND b.in_building_cluster = bc.building_cluster) = 1;"""
        self.cur.execute(query)
        try:
            count = self.cur.fetchone()[0]
        except:
            count = 0

        return count

    def dropIndoorTransformers(self):
        """
        Drop transformer if it is inside a building with zero load
        :return:
        """
        query = """WITH union_table (ungeom) AS 
                (SELECT ST_Union(geom) FROM buildings_tem WHERE peak_load_in_kw = 0)
            DELETE FROM buildings_tem WHERE ST_Within(center, (SELECT ungeom FROM union_table))
                AND type = 'Transformer';"""
        self.cur.execute(query)

    def updateLargeKmeansCluster(self, vertices, cluster_count):
        """
        Applies k-means clustering to large components and updated values in buildings_tem
        :param vertices:
        :param cluster_count:
        :return:
        """
        query = """
                WITH kmean AS (SELECT osm_id, ST_ClusterKMeans(center, %(ca)s)
                OVER() AS cid FROM buildings_tem WHERE vertice_id IN %(v)s),
                maxk AS (SELECT MAX(k_mean_cluster) AS max_k FROM buildings_tem)
            UPDATE buildings_tem b SET k_mean_cluster = (CASE 
            WHEN m.max_k ISNULL THEN k.cid + 1 
            ELSE m.max_k + k.cid + 1
            END)
            FROM kmean AS k, maxk AS m
            WHERE b.osm_id = k.osm_id;"""
        self.cur.execute(query, {"ca": cluster_count, "v": tuple(map(int, vertices))})

    def updateKmeansCluster(self, vertices):
        """
        Applies k-means clustering and updated values in buildings_tem
        :param vertices:
        :return:
        """
        query = """
                WITH maxk AS (SELECT MAX(k_mean_cluster) AS max_k FROM buildings_tem)
            UPDATE buildings_tem SET k_mean_cluster = (CASE 
            WHEN m.max_k ISNULL THEN 1 
            ELSE m.max_k + 1
            END)
            FROM maxk AS m
            WHERE vertice_id IN %(v)s;"""
        self.cur.execute(query, {"v": tuple(map(int, vertices))})

    def deleteUselessWays(self, vertices):
        """
        Deletes selected ways from ways_tem and ways_tem_vertices_pgr
        :param vertices:
        :return:
        """
        query = """DELETE FROM ways_tem WHERE target IN %(v)s;
                DELETE FROM ways_tem_vertices_pgr WHERE id IN %(v)s;"""
        self.cur.execute(query, {"v": tuple(map(int, vertices))})

    def deleteUselessTransformers(self, vertices):
        """
        Deletes selected transformers from buildings_tem
        :param vertices:
        :return:
        """
        query = """
                DELETE FROM buildings_tem WHERE vertice_id IN %(v)s;"""
        self.cur.execute(query, {"v": tuple(map(int, vertices))})

    def deleteIsolatedBuilding(self, plz, kcid):
        query = """DELETE FROM buildings_tem WHERE in_loadarea_cluster = %(p)s
                    AND k_mean_cluster = %(k)s AND in_building_cluster ISNULL;"""
        self.cur.execute(query, {"p": plz, "k": kcid})

    def saveInformationAndResetTables(self, plz):

        # Save results
        query = f"""
                INSERT INTO buildings_result 
                    SELECT '{conf_version.VERSION_ID}' as version_id, * FROM buildings_tem WHERE peak_load_in_kw != 0 AND peak_load_in_kw != -1;
                INSERT INTO ways_result
                    SELECT '{conf_version.VERSION_ID}' as version_id, * FROM ways_tem;"""
        self.cur.execute(query)

        # Set PLZ in ways_result
        query = f"""UPDATE ways_result SET plz = %(p)s WHERE plz ISNULL;"""
        self.cur.execute(query, vars={"p": plz})

        # Clear temporary tables
        query = """DELETE FROM buildings_tem"""
        self.cur.execute(query)
        print(self.cur.statusmessage)
        query = """DELETE FROM ways_tem"""
        self.cur.execute(query)
        print(self.cur.statusmessage)
        query = """DELETE FROM ways_tem_vertices_pgr"""
        self.cur.execute(query)
        print(self.cur.statusmessage)

        self.conn.commit()

    def removeDuplicateBuildings(self):
        """
        * Remove buildings without geometry or osm_id
        * Remove buildings which are duplicates of other buildings and have a copied id
        :return:
        """
        remove_query = """DELETE FROM buildings_tem WHERE geom ISNULL;"""
        self.cur.execute(remove_query)

        remove_noid_building = """DELETE FROM buildings_tem WHERE osm_id ISNULL;"""
        self.cur.execute(remove_noid_building)

        query = """DELETE FROM buildings_tem WHERE geom IN 
                    (SELECT geom FROM buildings_tem GROUP BY geom HAVING count(*) > 1) 
                    AND osm_id LIKE '%copy%';"""
        self.cur.execute(query)

    def getCableCostFromBcid(self, cable):
        cable_query = """SELECT cable FROM building_clusters 
                        WHERE version_id = %(v)s
                        AND loadarea_cluster = %(l)s
                        AND k_mean_cluster = %(k)s 
                        AND building_cluster = %(b)s;"""
        self.cur.execute(
            cable_query, {"v": conf_version.VERSION_ID, "l": cable[0], "k": cable[1], "b": cable[2]}
        )
        cost = self.cur.fetchone()[0]

        return cost

    def insert_version_if_not_exists(self):
        count_query = f"""SELECT COUNT(*) 
        FROM version 
        WHERE "version_id" = '{conf_version.VERSION_ID}'"""
        self.cur.execute(count_query)
        version_exists = self.cur.fetchone()[0]
        if version_exists:
            print(f"Version: {conf_version.VERSION_ID} (already exists)")
        else:
            # create new version
            consumer_categories_str = conf_version.CONSUMER_CATEGORIES.to_json().replace("'", "''")
            cable_cost_dict_str = json.dumps(conf_version.CABLE_COST_DICT).replace("'", "''")
            connection_available_cables_str = str(conf_version.CONNECTION_AVAILABLE_CABLES).replace(
                "'", "''"
            )
            other_parameters_dict = {
                "LARGE_COMPONENT_LOWER_BOUND": conf_version.LARGE_COMPONENT_LOWER_BOUND,
                "LARGE_COMPONENT_DIVIDER": conf_version.LARGE_COMPONENT_DIVIDER,
                "VN": conf_version.VN,
                "V_BAND_LOW": conf_version.V_BAND_LOW,
                "V_BAND_HIGH": conf_version.V_BAND_HIGH,
            }
            other_paramters_str = str(other_parameters_dict).replace("'", "''")

            insert_query = f"""INSERT INTO version (version_id, version_comment, consumer_categories, cable_cost_dict, connection_available_cables, other_parameters) VALUES
            ('{conf_version.VERSION_ID}', '{conf_version.VERSION_COMMENT}', '{consumer_categories_str}', '{cable_cost_dict_str}', '{connection_available_cables_str}', '{other_paramters_str}')"""
            self.cur.execute(insert_query)
            print(f"Version: {conf_version.VERSION_ID} (created for the first time)")

    def insert_parameter_tables(self, consumer_categories: pd.DataFrame):
        with self.sqla_engine.connect() as conn:
            conn.execute(text("DELETE FROM consumer_categories"))
            consumer_categories.to_sql(
                name="consumer_categories", con=conn, if_exists="append", index=False
            )

        print("Parameter tables are inserted")

    def analyse_basic_parameters(self, plz):
        cluster_list = self.getListFromPlz(plz)
        count = len(cluster_list)
        time = 0
        percent = 0

        load_count_dict = {}
        bus_count_dict = {}
        trafo_dict = {}
        print("start basic parameter counting")
        for kcid, bcid in cluster_list:
            load_count = 0
            bus_list = []
            try:
                net = self.read_net(plz, kcid, bcid)
                # net = pp.from_json(os.path.join(RESULT_DIR, "grid_genereation_result", file))
            except Exception as e:
                print(f" local network {kcid},{bcid} is problematic")
                raise e
            else:
                for row in net.load[["name", "bus"]].itertuples():
                    load_count += 1
                    bus_list.append(row.bus)
                bus_list = list(set(bus_list))
                bus_count = len(bus_list)

                for row in net.trafo[["sn_mva", "lv_bus"]].itertuples():
                    capacity = round(row.sn_mva * 1e3)

                    if capacity in trafo_dict:
                        trafo_dict[capacity] += 1

                        load_count_dict[capacity].append(load_count)
                        bus_count_dict[capacity].append(bus_count)
                    else:
                        trafo_dict[capacity] = 1

                        load_count_dict[capacity] = [load_count]
                        bus_count_dict[capacity] = [bus_count]

            time += 1
            if time / count >= 0.1:
                percent += 10
                print(f"{percent} percent finished")
                time = 0

        trafo_string = json.dumps(trafo_dict)
        load_count_string = json.dumps(load_count_dict)
        bus_count_string = json.dumps(bus_count_dict)

        print(bus_count_string)

        update_query = """INSERT INTO public.grid_parameters (version_id, plz, trafo_num, load_count_per_trafo, bus_count_per_trafo)
        VALUES(%s, %s, %s, %s, %s);"""
        self.cur.execute(
            update_query,
            vars=(conf_version.VERSION_ID, plz, trafo_string, load_count_string, bus_count_string),
        )

        print("basic parameter count finished")

    def analyse_cables(self, plz):
        cluster_list = self.getListFromPlz(plz)
        count = len(cluster_list)
        time = 0
        percent = 0

        # distributed according to cross_section
        cable_length_dict = {}
        for kcid, bcid in cluster_list:
            try:
                net = self.read_net(plz, kcid, bcid)
            except Exception as e:
                print(f" local network {kcid},{bcid} is problematic")
                raise e
            else:
                cable_df = net.line[net.line["in_service"] == True]

                cable_type = pd.unique(cable_df["std_type"]).tolist()
                for type in cable_type:

                    if type in cable_length_dict:
                        cable_length_dict[type] += (
                            cable_df[cable_df["std_type"] == type]["parallel"]
                            * cable_df[cable_df["std_type"] == type]["length_km"]
                        ).sum()

                    else:
                        cable_length_dict[type] = (
                            cable_df[cable_df["std_type"] == type]["parallel"]
                            * cable_df[cable_df["std_type"] == type]["length_km"]
                        ).sum()
            time += 1
            if time / count >= 0.1:
                percent += 10
                print(f"{percent} % processed")
                time = 0

        cable_length_string = json.dumps(cable_length_dict)

        update_query = """UPDATE public.grid_parameters
        SET cable_length = %(c)s 
        WHERE version_id = %(v)s AND plz = %(p)s;"""
        self.cur.execute(
            update_query, {"v": conf_version.VERSION_ID, "c": cable_length_string, "p": plz}
        )

        print("cable count finished")

    def analyse_per_trafo_parameters(self, plz):
        cluster_list = self.getListFromPlz(plz)
        count = len(cluster_list)
        time = 0
        percent = 0

        trafo_load_dict = {}
        trafo_max_distance_dict = {}
        trafo_avg_distance_dict = {}

        for kcid, bcid in cluster_list:
            try:
                net = self.read_net(plz, kcid, bcid)
            except Exception as e:
                print(f" local network {kcid},{bcid} is problematic")
                raise e
            else:
                trafo_sizes = net.trafo["sn_mva"].tolist()[0]

                load_bus = pd.unique(net.load["bus"]).tolist()

                top.create_nxgraph(net, respect_switches=False)
                all_distance = (
                    top.calc_distance_to_bus(
                        net,
                        net.trafo["lv_bus"].tolist()[0],
                        weight="weight",
                        respect_switches=False,
                    )
                    .loc[load_bus]
                    .tolist()
                )

                # calculate total sim_peak_load
                residential_bus_index = net.bus[
                    ~net.bus["zone"].isin(["Commercial", "Public"])
                ].index.tolist()
                commercial_bus_index = net.bus[
                    net.bus["zone"] == "Commercial"
                ].index.tolist()
                public_bus_index = net.bus[net.bus["zone"] == "Public"].index.tolist()

                residential_house_num = net.load[
                    net.load["bus"].isin(residential_bus_index)
                ].shape[0]
                public_house_num = net.load[
                    net.load["bus"].isin(public_bus_index)
                ].shape[0]
                commercial_house_num = net.load[
                    net.load["bus"].isin(commercial_bus_index)
                ].shape[0]

                residential_sum_load = (
                    net.load[net.load["bus"].isin(residential_bus_index)][
                        "max_p_mw"
                    ].sum()
                    * 1e3
                )
                public_sum_load = (
                    net.load[net.load["bus"].isin(public_bus_index)]["max_p_mw"].sum()
                    * 1e3
                )
                commercial_sum_load = (
                    net.load[net.load["bus"].isin(commercial_bus_index)][
                        "max_p_mw"
                    ].sum()
                    * 1e3
                )

                sim_peak_load = 0
                for building_type, sum_load, house_num in zip(
                    ["Residential", "Public", "Commercial"],
                    [residential_sum_load, public_sum_load, commercial_sum_load],
                    [residential_house_num, public_house_num, commercial_house_num],
                ):
                    if house_num:
                        sim_peak_load += utils.oneSimultaneousLoad(
                            installed_power=sum_load,
                            load_count=house_num,
                            sim_factor=conf_version.SIM_FACTOR[building_type],
                        )

                avg_distance = (sum(all_distance) / len(all_distance)) * 1e3
                max_distance = max(all_distance) * 1e3

                trafo_size = round(trafo_sizes * 1e3)

                if trafo_size in trafo_load_dict:
                    trafo_load_dict[trafo_size].append(sim_peak_load)

                    trafo_max_distance_dict[trafo_size].append(max_distance)

                    trafo_avg_distance_dict[trafo_size].append(avg_distance)
                else:
                    trafo_load_dict[trafo_size] = [sim_peak_load]
                    trafo_max_distance_dict[trafo_size] = [max_distance]
                    trafo_avg_distance_dict[trafo_size] = [avg_distance]

            time += 1
            if time / count >= 0.1:
                percent += 10
                print(f"{percent} % processed")
                time = 0

        trafo_load_string = json.dumps(trafo_load_dict)
        trafo_max_distance_string = json.dumps(trafo_max_distance_dict)
        trafo_avg_distance_string = json.dumps(trafo_avg_distance_dict)

        update_query = """UPDATE public.grid_parameters
        SET sim_peak_load_per_trafo = %(l)s, max_distance_per_trafo = %(m)s, avg_distance_per_trafo = %(a)s
        WHERE version_id = %(v)s AND plz = %(p)s;
        """
        self.cur.execute(
            update_query,
            {
                "v": conf_version.VERSION_ID,
                "p": plz,
                "l": trafo_load_string,
                "m": trafo_max_distance_string,
                "a": trafo_avg_distance_string,
            },
        )

        print("per trafo analysis finished")

    def read_trafo_dict(self, plz):  # TODO add plz
        read_query = """SELECT trafo_num FROM public.grid_parameters 
        WHERE version_id = %(v)s AND plz = %(p)s;"""
        self.cur.execute(read_query, {"v": conf_version.VERSION_ID, "p": plz})
        trafo_num_dict = self.cur.fetchall()[0][0]

        return trafo_num_dict

    def read_per_trafo_dict(self, plz):
        read_query = """SELECT load_count_per_trafo, bus_count_per_trafo, sim_peak_load_per_trafo,
        max_distance_per_trafo, avg_distance_per_trafo FROM public.grid_parameters 
        WHERE version_id = %(v)s AND plz = %(p)s;"""
        self.cur.execute(read_query, {"v": conf_version.VERSION_ID, "p": plz})
        result = self.cur.fetchall()

        load_dict = result[0][0]
        bus_dict = result[0][1]
        peak_dict = result[0][2]
        max_dict = result[0][3]
        avg_dict = result[0][4]

        return load_dict, bus_dict, peak_dict, max_dict, avg_dict

    def read_cable_dict(self, plz):
        read_query = """SELECT cable_length FROM public.grid_parameters
        WHERE version_id = %(v)s AND plz = %(p)s;"""
        self.cur.execute(read_query, {"v": conf_version.VERSION_ID, "p": plz})
        cable_length = self.cur.fetchall()[0][0]

        return cable_length

    def save_net(self, plz, kcid, bcid, json_string):
        insert_query = "INSERT INTO grids VALUES (%s, %s, %s, %s, %s)"
        self.cur.execute(insert_query, vars=(conf_version.VERSION_ID, plz, kcid, bcid, json_string))

    def read_net(self, plz, kcid, bcid):
        read_query = "SELECT grid FROM grids WHERE version_id=%s AND plz=%s AND kcid=%s AND bcid=%s LIMIT 1"
        self.cur.execute(read_query, vars=(conf_version.VERSION_ID, plz, kcid, bcid))
        grid_tuple = self.cur.fetchall()[0]
        grid_dict = grid_tuple[0]
        grid_json_string = json.dumps(grid_dict)
        net = pp.from_json_string(grid_json_string)

        return net

    # Getter functions with Geopandas

    def getGeoDataFrame(
        self,
        table,
        **kwargs,
    ) -> gpd.GeoDataFrame:
        """
        Args:
            **kwargs: equality filters matching with the table column names
        Returns: A geodataframe with all building information
        :param table: table name
        """
        version_id = conf_version.VERSION_ID

        if kwargs:
            if 'version_id' in kwargs:
                version_id = kwargs['version_id']
                kwargs.pop('version_id')
            filters = " AND " + " AND ".join(
                [f"{key} = {value}" for key, value in kwargs.items()]
            )
        else:
            filters = ""
        query = (
            f"""SELECT * FROM public.{table} 
                    WHERE version_id = %(v)s """
            + filters
        )

        params = {"v": version_id}
        with self.sqla_engine.begin() as connection:
            gdf = gpd.read_postgis(query, con=connection, params=params)

        return gdf


    def getAllVersionsofPLZ(self, plz):
        query = (
            f"""SELECT DISTINCT version_id FROM grids where PLZ=%(p)s"""
        )
        self.cur.execute(query, {"p": plz})
        result = self.cur.fetchall()
        return result


    def getAllNetsOfVersion(self, plz, version_id):
        query = (
            f"""SELECT kcid, bcid, grid
                FROM grids 
                WHERE plz=%(p)s AND version_id=%(v)s""")
        #(SELECT version_id FROM plz_version LIMIT 1)
        self.cur.execute(query, {"p": plz, "v": version_id})
        result = self.cur.fetchall()
        return result
    

    def copyPostcodeResultTableWithNewShape(self, plz, shape):
        """
        Copies the given plz entry from postcode to the postcode_result table
        :param plz:
        :return:
        """
        query = """INSERT INTO postcode_result (version_id, id, siedlungstyp, geom, hausabstand) 
                    VALUES(%(v)s, %(p)s::INT, null, ST_Multi(ST_Transform(ST_GeomFromGeoJSON(%(shape)s), 3035)), null)
                    ON CONFLICT (version_id,id) DO NOTHING;"""

        self.cur.execute(query, {"v": conf_version.VERSION_ID, "p": plz, "shape": shape})
        print(self.cur.statusmessage)

    #Pure Test method to make sure intersection between a GeoJSON shape and PLZ area geom data gives correct results
    def test__getPlzIntersectionFromShapefile(self, shape, **kwargs):
        """
        * Fills buildings_tem with residential buildings which are inside the selected polygon
        * Sets the loadarea cluster to first plz that intersects shapefile
        :param shape:
        :return:
        """

        # Fill table
        query = """SELECT plz FROM postcode WHERE ST_Intersects(postcode.geom, ST_Transform(ST_GeomFromGeoJSON(%(shape)s), 3035));"""
        self.cur.execute(query, {"v": conf_version.VERSION_ID, "shape": shape})
        print(self.cur.statusmessage)
        result = self.cur.fetchall()
        return result
    

    def test__getBuildingGeoJSONFromShapefile(self, building_type, shape, **kwargs):
        query = ""
        if building_type=='res':
            query = """SELECT json_build_object(
                    'type', 'FeatureCollection',
                    'features', json_agg(ST_AsGeoJSON(t.*)::json)
                )
                FROM (SELECT ST_Transform(geom, 4326), osm_id from res
                WHERE ST_Contains(ST_Transform(ST_GeomFromGeoJSON(%(shape)s), 3035), ST_Centroid(res.geom))) as t(geom, id);"""
        
        if building_type =='oth':
            query = """SELECT json_build_object(
                    'type', 'FeatureCollection',
                    'features', json_agg(ST_AsGeoJSON(t.*)::json)
                )
                FROM (SELECT ST_Transform(geom, 4326), osm_id from oth
                WHERE ST_Contains(ST_Transform(ST_GeomFromGeoJSON(%(shape)s), 3035), ST_Centroid(oth.geom))) as t(geom, id);"""

        self.cur.execute(query, {"v": conf_version.VERSION_ID, "shape": shape})
        print(self.cur.statusmessage)
        result = self.cur.fetchall()
        return result[0][0]
    
        
    def test__getBuildingGeoJSONFromTEM(self):

        query = """SELECT * from buildings_tem;"""
        
        self.cur.execute(query)
        print(self.cur.statusmessage)
        result = self.cur.fetchall()
        return result


    def test__getBuildingOfBus(self, plz,  lat, lon):

        query = """ SELECT  osm_id FROM buildings_result 
                    WHERE version_id=%(v)s AND in_loadarea_cluster=%(p)s AND
                    ST_CONTAINS(geom, ST_TRANSFORM(ST_SetSRID(ST_POINT(%(x)s, %(y)s), 4326), 3035))
                    """
        self.cur.execute(query, {"v": conf_version.VERSION_ID, "p": plz, "x": lat, "y": lon})
        result = self.cur.fetchall()
        if not result:
            return ()
        return result[0][0]
    
    def test_getAdditionalBuildingData(self, osm_id):
        query = """ SELECT build_res.area, 
                            peak_load_in_kw, 
                            res.use, 
                            oth.use, 
                            res.free_walls, 
                            oth.free_walls, 
                            build_res.floors, 
                            houses_per_building,
                            res.occupants,
                            res.refurb_wal,
                            res.refurb_roo,
                            res.refurb_bas,
                            res.refurb_win
                    FROM buildings_result build_res 
                    LEFT JOIN res ON build_res.osm_id=res.osm_id 
                    LEFT JOIN oth ON build_res.osm_id=oth.osm_id
                    WHERE build_res.osm_id=%(o)s LIMIT 1
            """
        self.cur.execute(query, {"o": osm_id})
        temp_result = self.cur.fetchall()[0]

        result = list(temp_result)
        if result[5] == None:
            del result[5]
        elif result[4] == None:
            del result[4]
            
        if result[3] == None:
            del result[3]
        elif result[2] == None:
            del result[2]

        return result