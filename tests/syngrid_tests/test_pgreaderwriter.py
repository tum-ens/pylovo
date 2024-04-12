import unittest, unittest.mock

from syngrid import pgReaderWriter
from syngrid.config_version import CONSUMER_CATEGORIES

import json
from pandapower import to_json
from numpy import nan

DBNAME = "pylovo_db_1_0_0_test"
VERSION_ID = "1.0"

class TestPgReaderWriter(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # configure this plz as according to which grid is generated
        # for the test database
        cls.plz = '89278'
        cls.api = pgReaderWriter.PgReaderWriter(dbname=DBNAME)

        #fail-proof method for sweeping tem tables between tests
        # cls.api.cur.execute("delete from buildings_tem;")
        # cls.api.conn.commit()


    @classmethod
    def tearDownClass(cls):
        # Disconnect from the database (if applicable)
        pass

    def tearDown(self):
        # rollback t
        self.api.conn.rollback()

    # Add test methods here, following the AAA pattern:
    # Arrange, Act, Assert

    def test_pg_readerwriter_initialized(self):
        """
        tests whether the pgReaderWriter is initialized correctly
        """
        self.assertIsNotNone(self.api.conn)
        self.assertIsNotNone(self.api.cur)

    def test_get_consumer_categories(self):
        expected_cols = [
            'id', 'definition', 'peak_load', 'yearly_consumption',
            'peak_load_per_m2', 'yearly_consumption_per_m2',
            'sim_factor'
        ]
        cc = self.api.get_consumer_categories()
        self.assertEqual(len(cc), 6)
        self.assertListEqual(expected_cols, cc.columns.to_list())

    def test_get_siedlungstyp_from_plz(self):
        """
        test if you can get siedlungstype from plz
        a grid must have been generated with the given plz
        """
        sdtyp = self.api.get_siedlungstyp_from_plz(self.plz)
        self.assertEqual(sdtyp, 2)

    def test_get_buildings_from_bc(self):
        # create sample buildings_tem to test
        sample_data = (
            ("Commercial", 1, 1, 1),
            ("Commercial", 1, 1, 2),
            ("Commercial", 1, 1, 2),
            ("Commercial", 2, 1, 3)
        )
        self.api.cur.executemany(
            "INSERT INTO buildings_tem (type, plz, bcid, kcid) VALUES (%s, %s, %s, %s);",
            sample_data
        )
        df = self.api.get_buildings_from_bc(1, 2, 1)
        self.assertEqual(2, len(df))

        res_0 = df[["plz", "bcid", "kcid"]].values[0].tolist()
        res_1 = df[["plz", "bcid", "kcid"]].values[1].tolist()
        expected_res = [1,1,2]
        self.assertListEqual(res_0, expected_res)
        self.assertListEqual(res_1, expected_res)

    def test_prepare_vertices_list(self):
        self.skipTest("calling many functions. can be tested by mocking cursor but will not be a good test")

    def test_get_vertices_from_bcid(self):
        self.skipTest("same case with prepare_vertices_list")

    def test_get_transformator_data(self):
        capacities, costs = self.api.get_transformator_data()

        for capacity in capacities:
            self.assertTrue(capacity in capacities)
            self.assertIsNotNone(costs[capacity])
        
        self.assertEqual(len(capacities), 4)

    def test_get_buildings_from_kc(self):
        self._insert_buildings_tem_with_plz_kcid_bcid()
        df = self.api.get_buildings_from_kc(2)
        expected_cols = [
            'osm_id', 'area', 'type', 'geom', 'houses_per_building', 
            'center', 'peak_load_in_kw', 'plz', 'vertice_id', 'bcid', 
            'kcid', 'floors', 'connection_point'
        ]
        self.assertListEqual(expected_cols, df.columns.to_list())
        # only 2 values should come with connection point is not none
        # and bcid is none, kcid == 2
        self.assertEqual(2, len(df))
        print(42)

    def test_get_vertices_from_connection_points(self):
        self._insert_buildings_tem_with_vertice_type()
        l = self.api.get_vertices_from_connection_points((2,3))
        self.assertListEqual(l, [2, 3,4])

    def test_get_smax_from_bcid(self):
        smax = self.api.get_smax_from_bcid(self.plz, 1, 3)     
        self.assertEqual(smax, 100)

    def test_get_list_from_plz(self):
        cluster_list = self.api.get_list_from_plz(self.plz)
        self.assertEqual(len(cluster_list), 70)

        prev_cl = (0,0)
        for cl in cluster_list:
            # test for the order by
            self.assertTrue(cl > prev_cl)
            prev_cl = cl
        
    def test_get_building_connection_points_from_bc(self):
        sample_data = (
            (100, None, 2, 3),
            (101, 1, 2, 3),
            (123, 1, 2, 3),
            (123, 1, 2, 3),
            (124, 1, 2, 4),
            (124, None, 2, 4),
        )
        self.api.cur.executemany(
            "INSERT INTO buildings_tem (connection_point, vertice_id, kcid, bcid) VALUES (%s, %s, %s, %s);",
            sample_data
        )
        cp = self.api.get_building_connection_points_from_bc(2, 3)
        # only distinct connection points therefore len = 2
        self.assertEqual(len(cp), 2)
        self.assertListEqual(cp, [101, 123])

    def test_get_single_connection_point_from_bc(self):
        sample_data = (
            (100, None, 2, 3),
            (101, 1, 2, 3),
            (102, 1, 2, 3),
        )
        self.api.cur.executemany(
            "INSERT INTO buildings_tem (connection_point, vertice_id, kcid, bcid) VALUES (%s, %s, %s, %s);",
            sample_data
        )
        cp = self.api.get_single_connection_point_from_bc(2, 3)
        self.assertEqual(cp, 101)

    def test_upsert_building_cluster(self):
        sample_data = (
            ("Commercial", 100, 1, None, 1),
            ("Commercial", 100, 1, None, 2),
            ("Commercial", 101, 1, None, 2),
            ("Commercial", 102, 1, None, 2),
            ("Commercial", 103, 2, None, 2)
        )
        self.api.cur.executemany(
            "INSERT INTO buildings_tem (type, connection_point, plz, bcid, kcid) VALUES (%s, %s, %s, %s, %s);",
            sample_data
        )

        new_bcid = 2
        vertices = [101, 102]
        s_max = 25
        self.api.upsert_building_cluster(1, 2, bcid=new_bcid, vertices=vertices, s_max=s_max)

        # check if null bcid's are updated with the new bcid=2
        self.api.cur.execute("select connection_point, plz, bcid, kcid from buildings_tem;")
        res = self.api.cur.fetchall()

        for pair in res:
            connection_point, _, bcid, _ = pair
            if connection_point in vertices:
                self.assertEqual(bcid, new_bcid)

        # check if new insertion worked
        self.api.cur.execute(
            "select plz, kcid, bcid, s_max from building_clusters where plz=1 and kcid=2 and bcid=2 and s_max=25;"
        )
        res = self.api.cur.fetchall()
        self.assertEqual(len(res), 1)
        self.assertListEqual(res, [(1,new_bcid,2,s_max)])
        
    def test_zero_too_large_consumers(self):
        sample_data = (
            ("Public", 250),
            ("Commercial", 150),
            ("Commercial", 100),
            ("SFH", 150),
            ("Public", 50),
            ("Commercial", 25)
        )
        self.api.cur.executemany(
            "INSERT INTO buildings_tem (type, peak_load_in_kw) VALUES (%s, %s);",
            sample_data
        )

        number = self.api.zero_too_large_consumers()
        self.assertEqual(number, 2)
        self.api.cur.execute("select type, peak_load_in_kw from buildings_tem;")
        rows = self.api.cur.fetchall()

        for row in rows:
            typ, load = row
            if typ in ("Public", "Commercial"):
                # loads are 0ed if they are more than 100 and type is matching
                self.assertLessEqual(load, 100)
        self.assertEqual(len(rows), 6)

    def test_clear_building_clusters_in_kmean_cluster(self):
        sample_data = (
            (VERSION_ID, 1, -1, 1),
            (VERSION_ID, 2, -1, -2),
            (VERSION_ID, 2, -1, -1),
            (VERSION_ID, 2, -1, 2),
            (VERSION_ID, 2, -1, 3),
            (VERSION_ID, 3, -2, 4)
        )
        self.api.cur.executemany(
            "INSERT INTO building_clusters (version_id, plz, kcid, bcid) VALUES (%s, %s, %s, %s);",
            sample_data
        )

        self.api.cur.execute("select * from building_clusters where plz = 2 and kcid = -1;")
        rows = self.api.cur.fetchall()
        self.assertEqual(len(rows), 4)
        self.api.clear_building_clusters_in_kmean_cluster(2, -1)
        self.api.cur.execute("select * from building_clusters where plz = 2 and kcid = -1;")
        rows = self.api.cur.fetchall()
        self.assertEqual(len(rows), 2)

    def test_count_kmean_cluster_consumers(self):
        sample_data = (
            (0, None, 0, "Transformer"),
            (0, None, 1, "NotTransformer"),
            (0, None, 2, "NotTransformer"),
            (0, 3, 3, "NotTransformer"),
            (1, 2, 3, "NotTransformer"),
            (1, 3, 4, "NotTransformer"),
        )
        self.api.cur.executemany(
            "INSERT INTO buildings_tem (kcid, bcid, vertice_id, type) VALUES (%s, %s, %s, %s);",
            sample_data
        )

        n = self.api.count_kmean_cluster_consumers(0)
        self.assertEqual(n, 2)

    def test_update_s_max(self):
        s_max = self.api.get_smax_from_bcid(self.plz, 1, 2)
        self.assertEqual(s_max, 250)
        self.api.update_s_max(self.plz, 1, 2, 0)
        s_max = self.api.get_smax_from_bcid(self.plz, 1, 2)
        self.assertEqual(s_max, 400)

        # for note != 0
        self.api.update_s_max(self.plz, 1, -11, 1)
        s_max = self.api.get_smax_from_bcid(self.plz, 1, -11)
        self.assertEqual(s_max, 630)


    def test_get_unfinished_bcids(self):
        res = self.api.get_unfinished_bcids(self.plz, 2)
        expected_bcids = [-12, -11, -10, -9, -8, -7, -6, -5, -4, -3, -2, -1] 
        self.assertListEqual(res, expected_bcids)

    def test_get_ont_info_from_bc(self):
        res = self.api.get_ont_info_from_bc(self.plz, 1, -1)

        self.assertEqual(res['ont_vertice_id'], 3461)
        self.assertEqual(res['s_max'], 250)

    def test_get_cables(self):
        df = self.api.get_cables((4,))
        expected_cols = [
            'name', 'max_i_a', 'r_mohm_per_km', 
            'x_mohm_per_km', 'z_mohm_per_km', 'kosten_eur'
        ]
        self.assertListEqual(expected_cols, df.columns.to_list())

        res = ['NAYY_4_120', 242, 225, 80, 239, 19]
        self.assertListEqual([res], df.values.tolist())

    def test_get_next_unfinished_kcid(self):
        sample_data = ((-2,),(-1,),(1,),(2,))
        self.api.cur.executemany(
            "INSERT INTO buildings_tem (kcid) VALUES (%s);",
            sample_data
        )
        kcid = self.api.get_next_unfinished_kcid(self.plz)

        # kcid = -2 does not exist in the other tables but in buildings_tem
        self.assertEqual(kcid, -2)

    def test_get_lcid_length(self):
        sample_data = ((0,),(5,),(-1,),(15,),(-1,))
        self.api.cur.executemany(
            "INSERT INTO loadarea (cluster_id) VALUES (%s);",
            sample_data
        )

        n = self.api.get_lcid_length()
        self.assertEqual(n, 3)

    def test_count_no_kmean_buildings(self):
        sample_data = (
            (0, None),
            (5, None),
            (0, 1),
            (15, 2),
            (10, None)
        )
        self.api.cur.executemany(
            "INSERT INTO buildings_tem (peak_load_in_kw, kcid) VALUES (%s, %s);",
            sample_data
        )

        self.assertEqual(2, self.api.count_no_kmean_buildings())

    def test_get_included_transformers(self):
        sample_data = (
            (1, 0, "Transformer"),
            (2, 1, "Transformer"),
            (2, 2, "Transformer"),
            (2, 3, "NotTransformer"),
            (3, 4, "NotTransformer"),
        )
        self.api.cur.executemany(
            "INSERT INTO buildings_tem (kcid, vertice_id, type) VALUES (%s, %s, %s);",
            sample_data
        )

        transformers = self.api.get_included_transformers(2)
        self.assertListEqual(transformers, [1,2])

    def test_get_kcid_length(self):
        sample_data = ((-2,),(-1,),(1,),(1,),(2,),(None,),(None,))
        self.api.cur.executemany(
            "INSERT INTO buildings_tem (kcid) VALUES (%s);",
            sample_data
        )
        self.assertEqual(self.api.get_kcid_length(), 4)        

    def test_copy_postcode_result_table(self):
        """
        test if copy_postcode_result_table is affected by casting
        """
        plz = "10"
        sample_data = (
            ("0.0", plz),
            ("1.0", plz)
        )
        self.assertEqual(self.api.count_postcode_result(plz), 0)
        self.api.cur.execute("select * from postcode_result;")
        len_postcode_results = len(self.api.cur.fetchall())
        self.api.cur.executemany(
            "INSERT INTO postcode_result (version_id, id) VALUES (%s, %s);",
            sample_data
        )
        self.assertEqual(self.api.count_postcode_result(plz), 1)
        # this will be copied to the postcode result table
        gid = -1
        self.api.cur.execute(
            "INSERT INTO postcode (plz, gid) VALUES (%s, %s);",
            (plz, gid)
        )
        
        self.api.copy_postcode_result_table(plz)
        # conflicting version id will not be added
        self.assertEqual(self.api.count_postcode_result(plz), 1)
        self.api.cur.execute("select * from postcode_result;")
        # only 2 rows added from the start
        self.assertEqual(len(self.api.cur.fetchall()), len_postcode_results + 2)
        gid = -2
        self.api.cur.execute(
            "INSERT INTO postcode (plz, gid) VALUES (%s, %s);",
            (plz, gid)
        )

        with unittest.mock.patch('syngrid.pgReaderWriter.VERSION_ID', "2"):
             self.api.copy_postcode_result_table(plz)
        self.api.cur.execute("select * from postcode_result;")
        # 4 lines added to the postcode_results from the start
        self.assertEqual(len(self.api.cur.fetchall()), len_postcode_results + 3)

    def test_count_postcode_result(self):
        self.assertEqual(self.api.count_postcode_result(self.plz), 1)

    def test_get_connected_component(self):
        self.api.set_ways_tem_table(self.plz)
        c, n = self.api.get_connected_component()
        self.assertEqual(len(c), 3964)
        self.assertEqual(len(n), 3964)

    def test_count_buildings_kcid(self):
        sample_data = (
            (100, None, 1, 3),
            (101, 1, 2, 3),
            (123, 1, 2, 3),
            (123, 1, 3, 3),
            (124, 1, 4, 4),
            (124, None, 4, 4),
        )
        self.api.cur.executemany(
            "INSERT INTO buildings_tem (connection_point, vertice_id, kcid, bcid) VALUES (%s, %s, %s, %s);",
            sample_data
        )

        self.assertEqual(self.api.count_buildings_kcid(2), 2)
        
    def test_set_ways_tem_table(self):
        n = self.api.set_ways_tem_table(self.plz)

        # can't test much on 3931 rows
        self.assertEqual(n, 4121)

    def test_insert_transformers(self):
        self.api.insert_transformers(self.plz)

        rows = self.api.get_all_buildings_tem()

        self.assertEqual(len(rows), 28)
        for row in rows:
            typ = row[2]
            plz = row[7]
            self.assertTrue(typ, "Transformer")
            self.assertTrue(plz, self.plz)

    def test_delete_transformers(self):
        with unittest.mock.patch.object(self.api, 'conn'):
            self.api.delete_transformers() 
            self.api.conn.commit.assert_called_once()
        self.api.cur.execute("select * from transformers;")
        rows = self.api.cur.fetchall()
        self.assertEqual(len(rows), 0)

    def test_generate_load_vector(self):
        sample_data = (
            (250, 101, 1, 1),
            (250, 101, 1, 2),
            (150, 102, 1, 2),
            (100, 102, 1, 2),
            (150, 102, 1, 4),
            (50, 103, 1, 5),
            (25, 104, 1, 5)
        )
        self.api.cur.executemany(
            "INSERT INTO buildings_tem (peak_load_in_kw, connection_point, kcid, bcid) VALUES (%s, %s, %s, %s);",
            sample_data
        )

        load = self.api.generate_load_vector(1, 2)
        self.assertListEqual(load.tolist(), [250, 250])

    def test_count_connected_buildings(self):
        sample_data = (
            (1, 0, "Transformer"),
            (2, 1, "Transformer"),
            (3, 2, "Transformer"),
            (2, 3, "NotTransformer"),
            (3, 4, "NotTransformer"),
            (3, 5, "NotTransformer"),
        )
        self.api.cur.executemany(
            "INSERT INTO buildings_tem (connection_point, vertice_id, type) VALUES (%s, %s, %s);",
            sample_data
        )

        self.assertEqual(self.api.count_connected_buildings((3,4)), 2)

    def test_update_ways_cost(self):
        self.api.set_ways_tem_table(self.plz)
        self.api.update_ways_cost()
        self.api.cur.execute("select cost, ST_Length(geom) from ways_tem;")

        rows = self.api.cur.fetchall()
        for row in rows:
            cost, geom_length = row
            self.assertEqual(cost, geom_length)

    def test_count_one_building_cluster(self):
        # buildings_tem is empty
        self.assertEqual(self.api.count_one_building_cluster(), 0)        
        sample_data = (
            ("Commercial", 1, 1, 1),
            ("Commercial", 1, 1, 2),
            ("Commercial", 1, 1, 2),
            ("Commercial", 2, 1, 3)
        )
        self.api.cur.executemany(
            "INSERT INTO buildings_tem (type, plz, bcid, kcid) VALUES (%s, %s, %s, %s);",
            sample_data
        )
        self.assertEqual(self.api.count_one_building_cluster(), 2)

    def test_update_kmeans_cluster(self):
        sample_data = (
            (1, 2, 3),
            (1, 2, 3),
            (2, 2, 3),
            (3, 2, 3),
            (3, 3, 4),
            (4, 5, 4),
        )
        self.api.cur.executemany(
            "INSERT INTO buildings_tem (vertice_id, kcid, bcid) VALUES (%s, %s, %s);",
            sample_data
        )
        vertices = [2,3,4]
        self.api.update_kmeans_cluster(vertices)
        self.api.cur.execute("select vertice_id, kcid from buildings_tem;")
        rows = self.api.cur.fetchall()

        for row in rows:
            vid, kcid = row
            if vid in vertices:
                self.assertEqual(kcid, 6) # max kcid + 1
            else:
                self.assertEqual(kcid, 2) # kcid's do not change

    def test_delete_ways(self):
        """
        Not really testing ways_tem_vertices_pgr table as of right now 
        """
        vertices = [1651252, 1753846]

        n = self.api.set_ways_tem_table(self.plz)
        self.assertEqual(n, 4121) # test whether table is set

        self.api.delete_ways(vertices) # 1 row
        self.api.cur.execute("select * from ways_tem;")
        rows = self.api.cur.fetchall()
        self.assertEqual(len(rows), n-2)

        self.api.cur.execute(
            "select * from ways_tem_vertices_pgr where id in %(v)s;", {"v": tuple(map(int, vertices))}
        )
        rows = self.api.cur.fetchall()
        self.assertEqual(len(rows), 0)

    def test_delete_transformers(self):
        self._insert_buildings_tem_with_vertice_type()
        rows = self.api.get_all_buildings_tem()
        n = len(rows)

        self.api.delete_transformers([1, 2])

        rows = self.api.get_all_buildings_tem()
        # 2 lines get deleted but currently not testing the types
        self.assertEqual(len(rows), n-3) 

    def test_delete_isolated_building(self):
        self._insert_buildings_tem_with_plz_kcid_bcid()

        rows = self.api.get_all_buildings_tem()
        self.assertEqual(len(rows), 8)

        self.api.delete_isolated_building(1, 2)
        rows = self.api.get_all_buildings_tem()
        self.assertEqual(len(rows), 5)

    def test_save_and_reset_tables(self):
        sample_data = (
            (1, 1, 1, 1, 0),
            (1, 1, 1, 2, -1),
            (2, 1, 1, 2, 5),
            (3, 2, 1, 3, 10),
            (4, 2, 1, 1, 5),
            (4, 2, 1, 2, 0),
            (5, 3, 1, 2, -1)
        )
        self.api.cur.executemany(
            "INSERT INTO buildings_tem (osm_id, plz, bcid, kcid, peak_load_in_kw) VALUES (%s, %s, %s, %s, %s);",
            sample_data
        )

        sample_data = ((1,0),(1,1),(1,2),(2,3),(2,4),(2,5),(None,6))
        self.api.cur.executemany(
            "INSERT INTO ways_tem (plz, id) VALUES (%s, %s);",
            sample_data
        )

        sample_data = ((0,),(1,),(2,),(3,),(4,),(5,),(6,))
        self.api.cur.executemany(
            "INSERT INTO ways_tem_vertices_pgr (id) VALUES (%s);",
            sample_data
        )

        with unittest.mock.patch.object(self.api, 'conn'):
            self.api.save_and_reset_tables(self.plz)

        self.api.cur.execute("select * from buildings_result where plz <= 5;")
        rows = self.api.cur.fetchall()
        self.assertEqual(len(rows), 3)

        self.api.cur.execute("select plz from ways_result where plz <= 5;")
        rows = self.api.cur.fetchall()
        self.assertEqual(len(rows), 6) # + 1 NULL filled with self.plz
        for row in rows:
            plz = row[0]
            self.assertIsNotNone(plz)


        # tables are emptied
        rows = self.api.get_all_buildings_tem()
        self.assertEqual(len(rows), 0)

        self.api.cur.execute("select plz from ways_tem;")
        rows = self.api.cur.fetchall()
        self.assertEqual(len(rows), 0)

        self.api.cur.execute("select * from ways_tem_vertices_pgr;")
        rows = self.api.cur.fetchall()
        self.assertEqual(len(rows), 0)


    def test_remove_duplicate_buildings(self):
        sample_data = (
            (1, 0, None),
            (2, 1, 'POLYGON((0 0,1 0,1 1,0 1,0 0))'),
            (2, '1_copy', 'POLYGON((0 0,1 0,1 1,0 1,0 0))'),
            (2, 2, 'POLYGON((0 0,1 0,1 2,0 2,0 0))'),
            (3, 2, None),
            (3, None, 'POLYGON((0 0,1 0,1 1,0 1,0 0))'),
        )
        self.api.cur.executemany(
            "INSERT INTO buildings_tem (connection_point, osm_id, geom) VALUES (%s, %s, %s);",
            sample_data
        )
        rows = self.api.get_all_buildings_tem()
        self.assertEqual(len(rows), 6)

        self.api.remove_duplicate_buildings()
        
        rows = self.api.get_all_buildings_tem()
        self.assertEqual(len(rows), 2)

    def test_insert_version_if_not_exists(self):
        self.api.insert_version_if_not_exists()

        self.api.cur.execute("select * from version;")
        rows = self.api.cur.fetchall()
        self.assertEqual(len(rows), 1)

        with unittest.mock.patch('syngrid.pgReaderWriter.VERSION_ID', "2"):
            self.api.insert_version_if_not_exists()
            self.api.cur.execute("select * from version;")
            rows = self.api.cur.fetchall()

            # new version added with version #2
            self.assertEqual(len(rows), 2)

    def test_insert_parameter_tables(self):
        self.api.insert_parameter_tables(CONSUMER_CATEGORIES)

        self.api.cur.execute("select * from consumer_categories;")
        rows = self.api.cur.fetchall()

        self.assertEqual(len(rows), 6)

        c_cats = CONSUMER_CATEGORIES.replace(nan, None)
        c_cats = c_cats.values.tolist()
        for l, r in zip(rows, c_cats):
            l = list(l)
            for i, _ in enumerate(l):
                try:
                    l[i] = float(l[i]) # cast decimals
                except TypeError:
                    # handle None
                    pass
                except ValueError:
                    # handle strings
                    pass
                finally:
                    self.assertEqual(l[i], r[i])

    def test_analyse_basic_parameters(self):
        with unittest.mock.patch.object(self.api, "insert_grid_parameters") as mock_insert:
            # this grid is generated in the test sample db so can not insert
            self.api.analyse_basic_parameters(self.plz)

            trafo_str = '{"100": 13, "250": 20, "630": 6, "400": 29, "160": 2}'
            load_ct_str = '''{"100": [2, 5, 1, 4, 18, 5, 11, 1, 4, 4, 1, 1, 2], 
            "250": [45, 4, 57, 74, 62, 53, 55, 60, 57, 53, 63, 3, 57, 43, 74, 67, 57, 66, 57, 44], 
            "630": [189, 155, 172, 109, 152, 115], 
            "400": [133, 92, 105, 109, 97, 124, 88, 131, 81, 143, 71, 82, 91, 109, 
            95, 140, 141, 110, 92, 88, 136, 79, 118, 90, 81, 81, 99, 135, 101], "160": [39, 21]}
            '''
            bus_ct_str = '''{"100": [2, 5, 1, 4, 7, 4, 6, 1, 1, 1, 1, 1, 2], 
            "250": [23, 4, 29, 62, 33, 27, 27, 17, 35, 28, 35, 3, 44, 29, 30, 29, 34, 39, 25, 13], 
            "630": [131, 64, 85, 66, 111, 39], 
            "400": [82, 48, 69, 65, 48, 72, 48, 74, 47, 66, 44, 22, 46, 44, 53, 76, 83, 53, 
            43, 23, 58, 50, 60, 77, 39, 47, 46, 71, 45],
            "160": [34, 16]}'''
            args_list = mock_insert.call_args_list[0][0] # tuple
            plz, tr_str, load_str, bus_str = args_list
            self.assertEqual(plz, self.plz)

            # dictionaries are packed with json, unpack with json for consistency
            self.assertEqual(json.loads(tr_str), json.loads(trafo_str))
            self.assertEqual(json.loads(load_str), json.loads(load_ct_str))
            self.assertEqual(json.loads(bus_str), json.loads(bus_ct_str))
    
    def test_read_trafo_dict(self):
        t_dict = self.api.read_trafo_dict(self.plz)
        expected_dict = {'100': 13, '160': 2, '250': 20, '400': 29, '630': 6}
        self.assertDictEqual(t_dict, expected_dict)

    def test_read_per_trafo_dict(self):
        d_list, d_labels, t_dict = self.api.read_per_trafo_dict(self.plz)
        self.assertEqual(len(d_list), 5)
        self.assertEqual(len(d_labels), 5)
        self.assertEqual(len(t_dict), 5)

        expected_dict = {'100': 13, '160': 2, '250': 20, '400': 29, '630': 6}
        self.assertDictEqual(t_dict, expected_dict)

    def test_read_cable_dict(self):
        cable_lengths = self.api.read_cable_dict(self.plz)
        expected = {
            'NYY 4x16 SE':215.5384041896998,
            'NYY 4x70 SE':25.074151817644108,
            'NYY 4x35 SE':81.83473461235606,
            'NYY 4x95 SE':10.690380378572794,
            'NAYY 4x185 SE':1.481499823357145
        }
        self.assertEqual(expected, cable_lengths)

    def test_save_and_read_net(self):
        """
        Not the best unit test design but since both functions are in
        a basic level they can be tested together
        """
        self.api.save_net(self.plz, -1, -1, to_json({1:1,2:2}, filename=None))
        
        net = self.api.read_net(self.plz, -1, -1)
        self.assertDictEqual(net, {'1': 1, '2':2})

    def test_get_grid_versions_with_plz(self):
        rows = self.api.get_grid_versions_with_plz(self.plz)
        
        # for these tests only one grid is generated and it has vertice_id of 1
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], '1.0')

    def test_get_grids_of_version(self):
        rows = self.api.get_grids_of_version(self.plz, VERSION_ID)
        self.assertEqual(len(rows), 70)

    def test_check_if_grid_exists(self):
        rows = self.api.check_if_grid_exists(self.plz, VERSION_ID)        
        self.assertEqual(len(rows), 1)

    def test_get_all_postcode_results(self):
        rows = self.api.get_all_postcode_results()
        self.assertEqual(len(rows), 1)

    def test_read_net_of_specific_version(self):
        grid = self.api.read_net_of_specific_version(self.plz, 1, 1, VERSION_ID)
        self.assertEqual(len(grid), 116)

    def test_get_number_of_households(self):
        self.assertEqual(self.api.get_number_of_households(self.plz), 5140)

    def test_get_number_of_houses(self):
        self.assertEqual(self.api.get_number_of_houses(self.plz), 2766)

    def test_get_dist_btw_houses(self):
        self.assertEqual(self.api.get_dist_btw_houses(self.plz), 34)

    def test_delete_plz_from_all_tables(self):
        """
        This does not delete from versions table
        """
        with unittest.mock.patch.object(self.api, 'conn') as mocked_conn:
            with unittest.mock.patch.object(self.api, 'cur') as mocked_cur:
                self.api.delete_plz_from_all_tables(self.plz , '1')
                self.assertEqual(mocked_cur.execute.call_count, 8)
                self.assertEqual(mocked_conn.commit.call_count, 8)

    def test_delete_version_from_all_tables(self):
        with unittest.mock.patch.object(self.api, 'conn') as mocked_conn:
            with unittest.mock.patch.object(self.api, 'cur') as mocked_cur:
                self.api.delete_version_from_all_tables('1')
                self.assertEqual(mocked_cur.execute.call_count, 9)
                self.assertEqual(mocked_conn.commit.call_count, 9)

    def test_get_municipal_register_for_plz(self):
        df = self.api.get_municipal_register_for_plz(self.plz)
        cols = [
            'plz', 'pop', 'area', 'lat', 'lon', 'ags', 
            'name_city', 'fed_state', 'regio7', 'regio5', 
            'pop_den'
        ]
        self.assertEqual(cols, df.columns.to_list())
        self.assertEqual(len(df), 1)

    def test_get_municipal_register(self):
        df = self.api.get_municipal_register()
        cols = [
            'plz', 'pop', 'area', 'lat', 'lon', 'ags', 
            'name_city', 'fed_state', 'regio7', 'regio5', 
            'pop_den'
        ]
        self.assertEqual(cols, df.columns.to_list())
        # sample db only has 1 grid generated
        self.assertEqual(len(df), 12866)

    def test_get_ags_log(self):
        df = self.api.get_ags_log()
        vals = df.values.tolist()
        self.assertListEqual(vals, [[9775134]])
        self.assertListEqual(['ags'], df.columns.to_list())

    def test_write_ags_log(self):
        ags = 123321
        with unittest.mock.patch.object(self.api, 'conn'):
            self.api.write_ags_log(ags)
            self.api.conn.commit.assert_called_once()
        df = self.api.get_ags_log()
        vals = df.values.tolist()
        self.assertTrue([ags] in vals)
        self.assertListEqual(['ags'], df.columns.to_list())

    def _insert_buildings_tem_with_vertice_type(self):
        sample_data = (
            (1, 0, "Transformer"),
            (2, 1, "Transformer"),
            (3, 2, "Transformer"),
            (3, 2, "NotTransformer"),
            (2, 3, "NotTransformer"),
            (3, 4, "NotTransformer"),
        )
        self.api.cur.executemany(
            "INSERT INTO buildings_tem (connection_point, vertice_id, type) VALUES (%s, %s, %s);",
            sample_data
        )

    def _insert_buildings_tem_with_plz_kcid_bcid(self):
        sample_data = (
            (None, 1, 2, 3),
            (None, 1, 2, 4),
            (None, 1, 2, 5),
            (None, 1, 2, None),
            (123, 1, 2, None),
            (124, 1, 2, None),
            (None, 1, 3, None),
            (None, 1, 4, None),
        )
        self.api.cur.executemany(
            "INSERT INTO buildings_tem (connection_point, plz, kcid, bcid) VALUES (%s, %s, %s, %s);",
            sample_data
        )

    def _fill_buildings_tem_table(self):
        """
        Copied function from GridGenerator.cache_and_preprocess_static_objects
        Call this only needed as this will create lots of functional dependencies to other functions
        and it is not ideal for a unit test
        """
        self.api.copy_postcode_result_table(self.plz)

        self.api.set_residential_buildings_table(self.plz)
        self.api.set_other_buildings_table(self.plz)
        self.api.remove_duplicate_buildings()

        self.api.set_loadarea_cluster_siedlungstyp(self.plz)

        _ = self.api.set_building_peak_load()
        _ = self.api.zero_too_large_consumers()

        self.api.assign_close_buildings()

        self.api.insert_transformers(self.plz)
        self.api.drop_indoor_transformers()


if __name__ == '__main__':
    unittest.main()