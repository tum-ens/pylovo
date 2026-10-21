"""Persisting and deleting generation results."""

from psycopg2 import sql

from pylovo import postcode_border
from pylovo.config_loader import VERSION_ID
from pylovo.database.base_mixin import BaseMixin, plz_table_name


class ResultsMixin(BaseMixin):
    """Copy PLZ working tables into the result tables and delete results again.

    Most result tables reference ``version``, ``postcode_result`` or ``grid_result`` with
    ``ON DELETE CASCADE`` (see ``config_table_structure.py``), so deleting a parent row removes
    all dependent rows.
    """

    def save_tables(self, plz: int) -> None:
        """Copy the buildings and ways of a PLZ from its working tables into the result tables.

        Buildings go to ``buildings_result`` (only rows with a load that belong to a generated
        grid), ways to ``ways_result``; both are tagged with the configured ``VERSION_ID``.
        Does not commit.

        Args:
            plz: Postcode whose working tables ``buildings_tem_<plz>`` and ``ways_tem_<plz>`` are saved.
        """
        buildings_table = sql.Identifier("pg_temp", plz_table_name("buildings_tem", plz))
        ways_table = sql.Identifier("pg_temp", plz_table_name("ways_tem", plz))

        # buildings_result is keyed by (version_id, objectid): keep one row per objectid
        # (the working table holds a single PLZ) and delete the duplicates.
        query = sql.SQL("""
                DELETE
                FROM {buildings} a USING (SELECT MIN(ctid) as ctid, objectid, plz
                                          FROM {buildings}
                                          GROUP BY (objectid, plz)
                                          HAVING COUNT(*) > 1) b
                WHERE a.objectid = b.objectid
                  AND a.plz = b.plz
                  AND a.ctid <> b.ctid;""").format(buildings=buildings_table)
        self.cur.execute(query)

        # Transformer rows (peak_load_in_kw = -1) and unloaded rows (0) are not saved.
        query = sql.SQL("""
            INSERT INTO pylovo.buildings_result
                (version_id, objectid, grid_result_id, id, feature_id, height, floor_area, floor_number,
                 residential_floor_area, nonresidential_floor_area, nonresidential_use, mix_score, mix_rule, mix_confidence,
                 building_use, building_use_id, building_type, type, occupants, households, construction_year,
                 postcode, address_street_id, street, house_number, geom, centroid, gemeindeschluessel,
                 changelog_id, assigned_way_id, residential_peak_load_in_kw, nonresidential_peak_load_in_kw,
                 nonresidential_mv_direct, peak_load_in_kw, vertice_id, connection_point, agg_connection_point)
                SELECT %(v)s as version_id, objectid, gr.grid_result_id, id, feature_id, height,
                       floor_area, floor_number, residential_floor_area, nonresidential_floor_area,
                       nonresidential_use, mix_score, mix_rule, mix_confidence, building_use, building_use_id, building_type,
                       type, occupants, households, bt.construction_year, postcode, address_street_id, street,
                       house_number, geom, centroid, gemeindeschluessel, changelog_id, assigned_way_id,
                       residential_peak_load_in_kw, nonresidential_peak_load_in_kw, nonresidential_mv_direct,
                       peak_load_in_kw, vertice_id, bt.connection_point, bt.agg_connection_point
            FROM {buildings} bt
            JOIN pylovo.grid_result gr
                ON bt.plz = gr.plz AND bt.kcid = gr.kcid AND bt.bcid = gr.bcid and gr.version_id = %(v)s
                WHERE peak_load_in_kw != 0 AND peak_load_in_kw != -1;""").format(buildings=buildings_table)
        self.cur.execute(query, {"v": VERSION_ID})

        # Routing across postcode borders saves only the ways touching the PLZ (the routing graph also held the
        # neighboring streets); split parts lose every marker column, so the filter is geometric.
        own_ways_filter = sql.SQL("""
                WHERE ST_Intersects(geom, (SELECT geom FROM pylovo.postcode_result
                                           WHERE version_id = %(v)s AND postcode_result_plz = %(p)s))""") \
            if postcode_border.enabled() else sql.SQL("")
        query = sql.SQL("""INSERT INTO pylovo.ways_result
                        SELECT %(v)s as version_id, clazz, source, target, cost, reverse_cost, geom, way_id,
                %(p)s as plz FROM {ways}{own_ways_filter};""").format(ways=ways_table, own_ways_filter=own_ways_filter)
        self.cur.execute(query, {"v": VERSION_ID, "p": plz})
        self.save_building_exclusions(plz)

    def save_building_exclusions(self, plz: int) -> None:
        """Record every loaded building of the PLZ that is in no grid, with the reason, in ``building_exclusion``.

        Removed rows come from ``excluded_tem`` (written by the removal steps). Rows still in
        ``buildings_tem`` but not saved are MV-direct (``peak_load_in_kw = 0``), in a street
        component that was dropped (no kcid) or not clustered. Must run after the buildings were
        saved. Does not commit.

        Args:
            plz: Postcode of the working tables.
        """
        excluded = sql.Identifier("pg_temp", plz_table_name("excluded_tem", plz))
        buildings = sql.Identifier("pg_temp", plz_table_name("buildings_tem", plz))
        query = sql.SQL("""
            INSERT INTO pylovo.building_exclusion (version_id, objectid, plz, reason)
            SELECT %(v)s, objectid, %(p)s, reason
            FROM {excluded}
            WHERE objectid IS NOT NULL
            UNION ALL
            SELECT %(v)s, bt.objectid, %(p)s,
                   CASE WHEN bt.peak_load_in_kw = 0 THEN 'mv_direct'
                        WHEN bt.kcid IS NULL THEN 'isolated_component'
                        ELSE 'not_clustered' END
            FROM {buildings} bt
            WHERE bt.objectid IS NOT NULL
              AND bt.peak_load_in_kw IS DISTINCT FROM -1
              AND bt.type IS DISTINCT FROM 'Transformer'
              AND NOT EXISTS (SELECT 1 FROM pylovo.buildings_result br
                              WHERE br.version_id = %(v)s AND br.objectid = bt.objectid)
            ON CONFLICT (version_id, objectid) DO NOTHING;""").format(excluded=excluded, buildings=buildings)
        self.cur.execute(query, {"v": VERSION_ID, "p": plz})

    def delete_plz_from_all_tables(self, plz: int, version_id: str) -> None:
        """Delete all results of one PLZ and version, refresh the materialized views and commit.

        First the MV variants of the version that were built for this PLZ are deleted with all
        their rows (``mv_variant`` cascades): MV grids span several PLZ, so removing only the nodes
        of this PLZ would leave rings with holes. Deleting the ``postcode_result`` row then cascades
        to ``grid_result`` and everything that references it (buildings, lines, pandapower tables,
        parameters, transformer positions).

        Args:
            plz: Postcode to delete.
            version_id: Version whose results are deleted.
        """
        self.cur.execute(
            "DELETE FROM pylovo.mv_variant WHERE version_id = %(v)s AND %(p)s = ANY(plz_list) "
            "RETURNING mv_variant_id;",
            {"v": version_id, "p": int(plz)},
        )
        mv_variants = sorted(row[0] for row in self.cur.fetchall())
        if mv_variants:
            self.logger.info(f"MV variant(s) {', '.join(mv_variants)} of version {version_id} deleted "
                             f"because they contain PLZ {plz}")
        delete_ways_query = """DELETE
               FROM pylovo.ways_result
                   WHERE version_id = %(v)s
                     AND plz = %(p)s;"""
        self.cur.execute(delete_ways_query, {"v": version_id, "p": int(plz)})
        self.cur.execute("DELETE FROM pylovo.mv_customers WHERE version_id = %(v)s AND plz = %(p)s;",
                         {"v": version_id, "p": int(plz)})

        query = """DELETE
               FROM pylovo.postcode_result
                   WHERE version_id = %(v)s
                     AND postcode_result_plz = %(p)s;"""
        self.cur.execute(query, {"v": version_id, "p": int(plz)})
        self.refresh_materialized_views()
        self.conn.commit()
        self.logger.info(f"All data for PLZ {plz} and version {version_id} deleted")

    def delete_versions_from_all_tables(self, version_ids: list[str]) -> int:
        """Delete versions with all their results, refresh the materialized views and commit.

        Args:
            version_ids: Versions to delete. All of them must exist.

        Returns:
            Number of deleted ``version`` rows.

        Raises:
            ValueError: If ``version_ids`` is empty or a version does not exist; nothing is deleted then.
        """
        if not version_ids:
            raise ValueError("At least one version ID must be provided.")

        self.cur.execute(
            "SELECT version_id FROM pylovo.version WHERE version_id = ANY(%(versions)s);",
            {"versions": version_ids},
        )
        existing_versions = {row[0] for row in self.cur.fetchall()}
        missing_versions = [version_id for version_id in version_ids if version_id not in existing_versions]
        if missing_versions:
            missing = ", ".join(missing_versions)
            raise ValueError(f"Version(s) not found in database: {missing}")

        # Every result table references pylovo.version (directly or through postcode_result or
        # grid_result) with ON DELETE CASCADE.
        query = "DELETE FROM pylovo.version WHERE version_id = ANY(%(versions)s);"
        self.cur.execute(query, {"versions": version_ids})
        deleted_count = self.cur.rowcount
        self.refresh_materialized_views()
        self.conn.commit()
        versions = ", ".join(version_ids)
        self.logger.info(f"Version(s) {versions} deleted from all tables")
        return deleted_count

    def delete_classification_version_from_related_tables(self, classification_id: int) -> None:
        """Delete a classification version, refresh the materialized views and commit.

        The rows of ``sample_set`` and ``transformer_classified`` of this version are removed
        by ``ON DELETE CASCADE``; the refresh removes them from ``transformer_classified_with_grid``.

        Args:
            classification_id: ID of the classification version to delete.

        Raises:
            ValueError: If the classification version does not exist.
        """
        query = "DELETE FROM pylovo.classification_version WHERE classification_id = %(cid)s;"
        self.cur.execute(query, {"cid": classification_id})
        if self.cur.rowcount == 0:
            self.conn.rollback()
            raise ValueError(f"Classification version not found in database: {classification_id}")
        self.refresh_materialized_views()
        self.conn.commit()

        self.logger.info(f"Deleted classification ID {classification_id}.")

    def delete_plz_from_sample_set_table(self, classification_id: str, plz: int) -> None:
        """Delete one PLZ of a classification version from ``sample_set`` and commit.

        Args:
            classification_id: ID of the classification version.
            plz: Postcode to remove.
        """
        query = """
                DELETE
            FROM pylovo.sample_set
                WHERE classification_id = %(cid)s
                  AND plz = %(p)s;
                """
        self.cur.execute(query, {"cid": classification_id, "p": plz})
        self.conn.commit()
        self.logger.info(f"Deleted PLZ {plz} for classification ID {classification_id} from sample_set table.")

    def delete_transformers(self) -> None:
        """Delete all rows of the raw transformer table ``pylovo.transformers`` and commit.

        Stored positions retain their geometry; their raw osm_id becomes NULL.
        DELETE runs in one transaction, unlike TRUNCATE with a referencing foreign key.
        """
        self.cur.execute("DELETE FROM pylovo.transformers;")
        self.conn.commit()
        self.logger.info('Transformers deleted.')
