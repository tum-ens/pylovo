"""Queries of the transformer editors: ``pylovo-api`` and the deprecated Flask map (``pylovo.data_import.transformers_ui``).

The UI edits the raw transformer candidates in ``pylovo.transformers``: it adds manual stations,
deletes stations and sets rated powers. Grid generation reads these rows when existing
transformer positions are enabled. Every method commits its own change because the UI opens a
short-lived client per request.
"""

import random
import time

from pylovo.config_loader import TARGET_EPSG
from pylovo.database.base_mixin import BaseMixin

# osm_ids of the raw transformers whose geometry intersects the PLZ polygon
# (pylovo.postcode.plz is unique, so every transformer appears at most once).
_TRANSFORMERS_IN_PLZ_SQL = """
    SELECT t.osm_id
    FROM pylovo.transformers t
    JOIN pylovo.postcode p ON ST_Intersects(t.geom, p.geom)
    WHERE p.plz = %(plz)s
"""


class TransformerUiMixin(BaseMixin):
    """Read and edit raw transformer candidates for the transformer map UI.

    The method names end in ``_trafo_ui`` because the frontend calls them by these names.
    """

    def get_transformer_positions_for_plz_trafo_ui(self, plz: int) -> list[dict]:
        """Return the raw transformers inside a PLZ (at most 1000).

        Args:
            plz: Postcode whose transformers are returned.

        Returns:
            One dict per transformer with the keys ``osm_id``, ``transformer_rated_power``,
            ``type``, ``geom_type``, ``within_shopping`` and ``geom_wkt`` (WGS84 WKT).
        """
        query = """
            SELECT
                t.osm_id,
                t.transformer_rated_power,
                t.type,
                t.geom_type,
                t.within_shopping,
                ST_AsText(ST_Transform(t.geom, 4326)) as geom_wkt
            FROM pylovo.transformers t
            JOIN pylovo.postcode p ON ST_Intersects(t.geom, p.geom)
            WHERE p.plz = %(plz)s
            LIMIT 1000
        """
        self.cur.execute(query, {"plz": plz})
        columns = [desc[0] for desc in self.cur.description]
        return [dict(zip(columns, row)) for row in self.cur.fetchall()]

    def add_transformer_position_trafo_ui(self, plz: int, geom_wkt: str, osm_id: str = None,
                                comment: str = "Manual", kcid: int = None, bcid: int = None,
                                transformer_rated_power: int = None) -> str:
        """Insert a manual transformer into ``pylovo.transformers`` and commit.

        Args:
            plz: Postcode of the UI view (not stored).
            geom_wkt: Point geometry as WGS84 WKT.
            osm_id: Identifier of the new row; defaults to ``manual/<unix time in ms>``.
            comment: Stored in the ``type`` column.
            kcid: Unused.
            bcid: Unused.
            transformer_rated_power: Rated power in kVA, or ``None`` if unknown.

        Returns:
            The ``osm_id`` of the new transformer.
        """
        if not osm_id:
            osm_id = f"manual/{time.time_ns() // 1_000_000}"

        transformer_query = f"""
            INSERT INTO pylovo.transformers (osm_id, type, transformer_rated_power, geom_type, within_shopping, osm, lod2, geom)
            VALUES (
                %(osm_id)s,
                %(type)s,
                %(transformer_rated_power)s,
                %(geom_type)s,
                %(within_shopping)s,
                true,
                false,
                ST_Multi(ST_Transform(ST_GeomFromText(%(geom_wkt)s, 4326), {TARGET_EPSG}))
            )
            RETURNING osm_id
        """
        self.cur.execute(transformer_query, {
            "osm_id": osm_id,
            "type": comment,
            "transformer_rated_power": transformer_rated_power,
            "geom_type": "manual",
            "within_shopping": False,
            "geom_wkt": geom_wkt
        })
        self.conn.commit()

        return osm_id

    def delete_transformer_position_trafo_ui(self, grid_result_id: int) -> bool:
        """Delete the transformer position of one generated grid and commit.

        Only the ``transformer_positions`` row is removed; the ``grid_result`` row stays.

        Args:
            grid_result_id: Grid whose transformer position is deleted.

        Returns:
            True if a row was deleted.
        """
        delete_query = "DELETE FROM pylovo.transformer_positions WHERE grid_result_id = %(grid_result_id)s"
        self.cur.execute(delete_query, {"grid_result_id": grid_result_id})
        deleted = self.cur.rowcount > 0
        if deleted:
            self.conn.commit()
        return deleted

    def delete_transformer_by_osm_id_trafo_ui(self, osm_id: str) -> bool:
        """Delete a raw transformer and commit.

        ``ON DELETE CASCADE`` also removes the ``transformer_positions`` rows of generated grids
        that used this transformer.

        Args:
            osm_id: Identifier of the transformer.

        Returns:
            True if the transformer existed and was deleted.
        """
        delete_query = "DELETE FROM pylovo.transformers WHERE osm_id = %(osm_id)s"
        self.cur.execute(delete_query, {"osm_id": osm_id})
        rows_affected = self.cur.rowcount
        self.logger.debug(f"Deleting transformer {osm_id} affected {rows_affected} rows")

        if rows_affected > 0:
            self.conn.commit()

        return rows_affected > 0

    def clear_capacities_trafo_ui(self, plz: int) -> bool:
        """Set the rated power of all raw transformers inside a PLZ to NULL and commit.

        Args:
            plz: Postcode.

        Returns:
            True on success, False if the update failed (it is rolled back).
        """
        try:
            query = f"""
                UPDATE pylovo.transformers
                SET transformer_rated_power = NULL
                WHERE osm_id IN ({_TRANSFORMERS_IN_PLZ_SQL})
            """
            self.cur.execute(query, {"plz": plz})
            self.logger.info(f"Cleared capacities for {self.cur.rowcount} transformers")
            self.conn.commit()
            return True
        except Exception as e:
            self.conn.rollback()
            self.logger.error(f"Error in clear_capacities_trafo_ui: {e}")
            return False

    def get_plz_bounds_trafo_ui(self, plz: int) -> dict | None:
        """Return the WGS84 bounding box of a PLZ.

        Args:
            plz: Postcode.

        Returns:
            Dict with ``minx``, ``miny``, ``maxx`` and ``maxy``, or ``None`` if the PLZ is unknown.
        """
        query = """
            SELECT ST_XMin(ST_Transform(geom, 4326)) as minx, ST_YMin(ST_Transform(geom, 4326)) as miny,
                   ST_XMax(ST_Transform(geom, 4326)) as maxx, ST_YMax(ST_Transform(geom, 4326)) as maxy
            FROM pylovo.postcode
            WHERE plz = %(plz)s
        """
        self.cur.execute(query, {"plz": plz})
        row = self.cur.fetchone()
        if row:
            return {
                "minx": float(row[0]),
                "miny": float(row[1]),
                "maxx": float(row[2]),
                "maxy": float(row[3])
            }
        return None

    def get_available_plz_list_trafo_ui(self) -> list[int]:
        """Return all PLZ of the local ``postcode`` table in ascending order (not only generated ones)."""
        query = """
            SELECT DISTINCT plz
            FROM pylovo.postcode
            ORDER BY plz
        """
        self.cur.execute(query)
        return [row[0] for row in self.cur.fetchall()]

    def update_transformer_capacity_trafo_ui(self, osm_id: str, transformer_rated_power: int) -> bool:
        """Set the rated power of one raw transformer and commit.

        Args:
            osm_id: Identifier of the transformer.
            transformer_rated_power: Rated power in kVA.

        Returns:
            True if the transformer exists.
        """
        query = """
            UPDATE pylovo.transformers
            SET transformer_rated_power = %(transformer_rated_power)s
            WHERE osm_id = %(osm_id)s
        """
        self.cur.execute(query, {"osm_id": osm_id, "transformer_rated_power": transformer_rated_power})
        self.conn.commit()
        return self.cur.rowcount > 0

    def bulk_update_capacities_uniform_trafo_ui(self, plz: int, transformer_rated_power: int) -> bool:
        """Set all raw transformers inside a PLZ to the same rated power and commit.

        Args:
            plz: Postcode.
            transformer_rated_power: Rated power in kVA.

        Returns:
            True if at least one transformer was updated; False if the PLZ has none or the
            update failed (it is rolled back).
        """
        try:
            self.cur.execute(f"SELECT COUNT(*) FROM ({_TRANSFORMERS_IN_PLZ_SQL}) AS t", {"plz": plz})
            count = self.cur.fetchone()[0]
            self.logger.info(f"Found {count} transformers in PLZ {plz}")

            if count == 0:
                return False

            query = f"""
                UPDATE pylovo.transformers
                SET transformer_rated_power = %(transformer_rated_power)s
                WHERE osm_id IN ({_TRANSFORMERS_IN_PLZ_SQL})
            """
            self.cur.execute(query, {"plz": plz, "transformer_rated_power": transformer_rated_power})
            rows_updated = self.cur.rowcount
            self.logger.info(f"Updated {rows_updated} transformers")
            self.conn.commit()
            return rows_updated > 0
        except Exception as e:
            self.conn.rollback()
            self.logger.error(f"Error in bulk_update_capacities_uniform_trafo_ui: {e}")
            return False

    def bulk_update_capacities_percentage_trafo_ui(self, plz: int, capacity_distribution: dict) -> bool:
        """Assign rated powers to the raw transformers of a PLZ by percentage and commit.

        Each capacity gets ``floor(n * percentage / 100)`` transformers; the rounding remainder
        gets the capacity with the largest percentage. The assignment is shuffled randomly
        (unseeded).

        Args:
            plz: Postcode.
            capacity_distribution: Percentages by rated power in kVA, e.g.
                ``{400: 30, 630: 50, 1000: 20}``.

        Returns:
            True on success; False if the PLZ has no transformers or the update failed
            (it is rolled back).
        """
        try:
            self.cur.execute(_TRANSFORMERS_IN_PLZ_SQL, {"plz": plz})
            transformer_ids = [row[0] for row in self.cur.fetchall()]
            self.logger.info(f"Found {len(transformer_ids)} transformers for percentage distribution")

            if not transformer_ids:
                return False

            capacity_list = []
            for capacity, percentage in capacity_distribution.items():
                if percentage > 0:
                    count = int(len(transformer_ids) * percentage / 100)
                    capacity_list.extend([capacity] * count)

            if len(capacity_list) < len(transformer_ids):
                most_common_capacity = max(capacity_distribution.keys(), key=lambda k: capacity_distribution[k])
                remaining = len(transformer_ids) - len(capacity_list)
                capacity_list.extend([most_common_capacity] * remaining)

            random.shuffle(capacity_list)

            update_query = """
                UPDATE pylovo.transformers
                SET transformer_rated_power = %(transformer_rated_power)s
                WHERE osm_id = %(osm_id)s
            """
            # Percentages summing to more than 100 produce a longer list; the surplus is ignored.
            for osm_id, capacity in zip(transformer_ids, capacity_list):
                self.cur.execute(update_query, {"osm_id": osm_id, "transformer_rated_power": capacity})

            self.logger.info(f"Updated {min(len(transformer_ids), len(capacity_list))} transformers with new capacities")
            self.conn.commit()
            return True
        except Exception as e:
            self.conn.rollback()
            self.logger.error(f"Error in bulk_update_capacities_percentage_trafo_ui: {e}")
            return False
