"""Persisting and deleting generation results."""

from psycopg2 import sql

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
        buildings_table = sql.Identifier("pylovo", plz_table_name("buildings_tem", plz))
        ways_table = sql.Identifier("pylovo", plz_table_name("ways_tem", plz))

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

        query = sql.SQL("""INSERT INTO pylovo.ways_result
                        SELECT %(v)s as version_id, clazz, source, target, cost, reverse_cost, geom, way_id,
                %(p)s as plz FROM {ways};""").format(ways=ways_table)
        self.cur.execute(query, {"v": VERSION_ID, "p": plz})

    def delete_plz_from_all_tables(self, plz: int, version_id: str) -> None:
        """Delete all results of one PLZ and version, refresh the materialized views and commit.

        Deleting the ``postcode_result`` row cascades to ``grid_result`` and everything that
        references it (buildings, lines, pandapower tables, parameters, transformer positions).

        Args:
            plz: Postcode to delete.
            version_id: Version whose results are deleted.
        """
        delete_ways_query = """DELETE
               FROM pylovo.ways_result
                   WHERE version_id = %(v)s
                     AND plz = %(p)s;"""
        self.cur.execute(delete_ways_query, {"v": version_id, "p": int(plz)})

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

    def delete_classification_version_from_related_tables(self, classification_id: str) -> None:
        """Delete a classification version and commit.

        The rows of ``sample_set`` and ``transformer_classified`` of this version are removed
        by ``ON DELETE CASCADE``.

        Args:
            classification_id: ID of the classification version to delete.
        """
        query = "DELETE FROM pylovo.classification_version WHERE classification_id = %(cid)s;"
        self.cur.execute(query, {"cid": classification_id})
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

        ``transformer_positions`` of generated grids reference these rows. Deleting them would
        cascade into the stored results, so the method refuses while such rows exist.
        (``TRUNCATE`` is never possible: PostgreSQL rejects it for any table referenced by a
        foreign key, even when the referencing table is empty.)

        Raises:
            ValueError: If generated grids still reference transformers; delete those versions
                first (``pylovo-delete --version ...``).
        """
        self.cur.execute("SELECT COUNT(*) FROM pylovo.transformer_positions;")
        referenced = self.cur.fetchone()[0]
        if referenced:
            raise ValueError(
                f"{referenced} transformer positions of generated grids reference pylovo.transformers. "
                "Delete those versions first (pylovo-delete --version <id>), then retry."
            )
        self.cur.execute("DELETE FROM pylovo.transformers;")
        self.conn.commit()
        self.logger.info('Transformers deleted.')
