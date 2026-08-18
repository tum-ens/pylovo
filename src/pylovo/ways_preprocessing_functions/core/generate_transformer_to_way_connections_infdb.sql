/*
 * PURPOSE
 * -------
 * Connect only transformers to the existing ways network for USE_INFDB=True.
 * Buildings are assumed to be already connected upstream in the InfDB flow.
 *
 * INPUT
 * -----
 * - buildings_tem: transformer points (type = 'Transformer')
 * - ways_tem: current ways network
 *
 * OUTPUT
 * ------
 * - Adds transformer connection lines (clazz 110) to ways_tem
 * - Splits affected ways into segments (clazz 103)
 */

CREATE OR REPLACE FUNCTION generate_transformer_to_way_connections_infdb() RETURNS void
LANGUAGE plpgsql AS
$$
DECLARE
    r RECORD;
    s RECORD;
    final_geom geometry;
    part geometry;

BEGIN
    -- Build transformer-only connection candidates.
    DROP TABLE IF EXISTS temp_transformer_connection_candidates_infdb;
    CREATE TEMP TABLE temp_transformer_connection_candidates_infdb AS
    WITH transformers AS (
        SELECT objectid, centroid
        FROM buildings_tem
        WHERE type = 'Transformer'
    ),
    closest_way AS (
        SELECT
            t.objectid,
            t.centroid,
            w.way_id AS old_way_id,
            w.geom AS old_geom,
            ST_ShortestLine(t.centroid, w.geom) AS new_geom
        FROM transformers t
        JOIN LATERAL (
            SELECT way_id, geom
            FROM ways_tem w
            WHERE w.clazz != 110
              AND ST_DWithin(t.centroid, w.geom, 2000)
              AND ST_Distance(t.centroid, w.geom) > 0.1
            ORDER BY t.centroid <-> w.geom
            LIMIT 1
        ) w ON TRUE
    )
    SELECT
        c.objectid,
        c.centroid,
        c.old_way_id,
        c.old_geom,
        c.new_geom,
        ST_ClosestPoint(c.old_geom, c.new_geom) AS connection_point
    FROM closest_way c;

    CREATE INDEX temp_transformer_candidates_old_way_idx
        ON temp_transformer_connection_candidates_infdb (old_way_id);
    CREATE INDEX temp_transformer_candidates_connection_gix
        ON temp_transformer_connection_candidates_infdb USING GIST (connection_point);

    -- Insert transformer connection lines. If close to endpoints, no split is needed.
    FOR r IN SELECT * FROM temp_transformer_connection_candidates_infdb
    LOOP
        IF ST_Distance(ST_StartPoint(r.old_geom), r.connection_point) < 0.1 THEN
            final_geom := ST_MakeLine(r.centroid, ST_StartPoint(r.old_geom));
            PERFORM insert_way_segment(110, final_geom);
            DELETE FROM temp_transformer_connection_candidates_infdb
            WHERE objectid = r.objectid;

        ELSIF ST_Distance(ST_EndPoint(r.old_geom), r.connection_point) < 0.1 THEN
            final_geom := ST_MakeLine(r.centroid, ST_EndPoint(r.old_geom));
            PERFORM insert_way_segment(110, final_geom);
            DELETE FROM temp_transformer_connection_candidates_infdb
            WHERE objectid = r.objectid;

        ELSE
            final_geom := r.new_geom;
            PERFORM insert_way_segment(110, final_geom);
        END IF;
    END LOOP;

    DROP TABLE IF EXISTS grouped_splits;
    CREATE TEMP TABLE grouped_splits AS
    SELECT
        old_way_id,
        old_geom,
        ARRAY_AGG(connection_point ORDER BY ST_LineLocatePoint(old_geom, connection_point)) AS connection_points
    FROM temp_transformer_connection_candidates_infdb
    GROUP BY old_way_id, old_geom;

    -- Split ways at transformer connection points.
    FOR s IN SELECT * FROM grouped_splits LOOP
        DELETE FROM ways_tem WHERE way_id = s.old_way_id;

        FOR part IN SELECT * FROM split_way_at_connection_points(s.old_geom, s.connection_points)
        LOOP
            PERFORM insert_way_segment(103, part);
        END LOOP;
    END LOOP;

END;
$$;