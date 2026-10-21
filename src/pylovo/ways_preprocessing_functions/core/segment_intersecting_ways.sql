--
-- Name: segment_intersecting_ways(); Type: FUNCTION
--

/*
 * FUNCTION PURPOSE AND OVERVIEW:
 * =============================
 * This function processes a ways network to create proper geometric connections
 * at intersection points. It takes overlapping or crossing line geometries and splits
 * them at their intersection points, ensuring that the network topology is properly
 * segmented.
 * 
 * The main goal is to convert a network where ways may cross without being 
 * topologically connected into a properly segmented network where intersections
 * are represented by shared endpoints between line segments.
 *
 * ALGORITHM OVERVIEW:
 * 1. Iterate through each way of the current ways_tem table (one pass)
 * 2. Find another way that crosses the current way in one point
 * 3. Calculate the precise intersection point between the two ways
 * 4. Split both intersecting ways at the intersection point
 * 5. Replace original ways with their split segments
 * 6. Repeat the pass on the updated table until a pass splits nothing
 *
 * INPUT: Uses data from 'ways_tem' table containing geometric line data
 * OUTPUT: Modified 'ways_tem' table with properly segmented line geometries
 */

CREATE OR REPLACE FUNCTION segment_intersecting_ways() RETURNS void
    LANGUAGE PLPGSQL
AS
$$
declare
    -- Id of the way being processed and its current row (geometry, classification, ID)
    current_id        integer;
    way               RECORD;
    -- Record to store the calculated intersection point geometry
    interpolate_point RECORD;
    -- Record to hold the intersecting street found for current way
    old_street        RECORD;
    -- Splits of the current pass; the passes repeat until one splits nothing
    splits            integer;
    passes            integer := 0;
begin
    -- Every pass re-reads ways_tem: a way deleted earlier in the pass is skipped (an id that a new part
    -- took over is processed with the part's current geometry), and the parts inserted in a pass are
    -- checked in the next one. So every crossing is split once, also several crossings of one way, and
    -- no deleted geometry is split again.
    loop
        splits := 0;
        passes := passes + 1;
        for current_id in SELECT way_id FROM ways_tem ORDER BY way_id
            loop
                SELECT geom, clazz, way_id INTO way FROM ways_tem WHERE way_id = current_id;
                IF NOT FOUND OR ST_Length(way.geom) = 0 THEN
                    continue;
                END IF;

                -- STEP 1: FIND INTERSECTING STREET
                -- Another way that crosses the middle 98% of the current way (the first and last 1% are
                -- left out to avoid false intersections at the end points) in exactly one point: an
                -- overlapping or twice-crossing way cannot be split at one point. The intersection of
                -- the buffered current way with it must be a LineString for ST_LineInterpolatePoint.
                SELECT geom, clazz, way_id
                INTO old_street
                FROM ways_tem as w
                WHERE ST_Intersects(ST_LineSubstring(way.geom, 0.01, 0.99), w.geom)
                  AND w.way_id != way.way_id
                  AND ST_GeometryType(ST_Intersection(ST_LineSubstring(way.geom, 0.01, 0.99), w.geom)) = 'ST_Point'
                  AND ST_GeometryType(ST_Intersection(ST_Buffer(way.geom, 0.1), w.geom)) = 'ST_LineString'
                ORDER BY w.way_id
                LIMIT 1;

                IF NOT FOUND THEN
                    continue;
                END IF;

                -- STEP 2: CALCULATE INTERSECTION POINT
                -- The midpoint of the part of the old street inside the 0.1 m buffer of the current way
                SELECT ST_LineInterpolatePoint(ST_Intersection(ST_Buffer(way.geom, 0.1), old_street.geom), 0.5) AS geom
                INTO interpolate_point;

                -- STEP 3: REPLACE THE OLD STREET BY ITS TWO PARTS AT THE INTERSECTION POINT
                DELETE
                FROM ways_tem
                WHERE way_id = old_street.way_id;
                PERFORM insert_way_segment(
                    old_street.clazz,
                    ST_LineSubstring(old_street.geom, 0, ST_LineLocatePoint(
                        old_street.geom, interpolate_point.geom))
                );
                PERFORM insert_way_segment(
                    old_street.clazz,
                    ST_LineSubstring(old_street.geom, ST_LineLocatePoint(
                        old_street.geom, interpolate_point.geom), 1)
                );

                -- STEP 4: REPLACE THE CURRENT WAY BY ITS TWO PARTS AT THE INTERSECTION POINT
                DELETE FROM ways_tem WHERE way_id = way.way_id;
                PERFORM insert_way_segment(
                    way.clazz,
                    ST_LineSubstring(way.geom, 0, ST_LineLocatePoint(
                        way.geom, interpolate_point.geom))
                );
                PERFORM insert_way_segment(
                    way.clazz,
                    ST_LineSubstring(way.geom, ST_LineLocatePoint(
                        way.geom, interpolate_point.geom), 1)
                );
                splits := splits + 1;
            end loop;
        EXIT WHEN splits = 0;
        -- Safety net only: every split leaves the parts meeting at the split point, outside their middle 98%.
        IF passes >= 100 THEN
            RAISE WARNING 'segment_intersecting_ways stopped after % passes with crossings left', passes;
            EXIT;
        END IF;
    end loop;

    RAISE NOTICE 'Way connections have been successfully drawn and segmented.';
end;
$$;
