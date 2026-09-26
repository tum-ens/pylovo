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
 * 1. Iterate through each way in the ways_tem table
 * 2. Find another way that intersects with the current way
 * 3. Calculate the precise intersection point between the two ways
 * 4. Split both intersecting ways at the intersection point
 * 5. Replace original ways with their split segments
 * 6. Continue until all intersections are processed
 *
 * INPUT: Uses data from 'ways_tem' table containing geometric line data
 * OUTPUT: Modified 'ways_tem' table with properly segmented line geometries
 */

CREATE OR REPLACE FUNCTION segment_intersecting_ways() RETURNS void
    LANGUAGE PLPGSQL
AS
$$
declare
    -- Record to hold current way being processed (geometry, classification, ID)
    way               RECORD;
    
    -- Record to store the calculated intersection point geometry
    interpolate_point RECORD;
    
    -- Record to hold the intersecting street found for current way
    old_street        RECORD;
    
    
begin
    -- MAIN PROCESSING LOOP: Iterate through all ways in the temporary table
    -- Each iteration processes one way and finds its intersections with other ways
    for way in
        SELECT geom, clazz, way_id
        FROM ways_tem
        loop
            -- STEP 1: FIND INTERSECTING STREET
            -- Search for another way that intersects with the current way
            -- We use ST_LineSubstring(0.01, 0.99) to exclude the first and last 1% 
            -- of the line to avoid false intersections at endpoints
            SELECT geom, clazz, way_id
            INTO old_street
            FROM ways_tem as w
            WHERE ST_Intersects(ST_LineSubstring(way.geom, 0.01, 0.99), w.geom) -- Limit to middle 98% of line
              AND w.way_id != way.way_id  -- Exclude the current way from intersection search
            LIMIT 1; -- Only process one intersection at a time

            -- STEP 2: VALIDATION CHECK
            -- If no intersecting street is found, skip to next way
            IF NOT FOUND THEN
                continue;
            END IF;

            -- STEP 3: GEOMETRY TYPE VALIDATION
            -- Verify that the intersection of the buffered way and old street creates a LineString
            -- ST_Buffer(way.geom, 0.1) creates a small buffer around the current way
            -- The intersection must be a LineString for ST_LineInterpolatePoint to work properly
            IF ST_Geometrytype(ST_Intersection(ST_Buffer(way.geom, 0.1), old_street.geom)) != 'ST_LineString' THEN
                -- Log problematic geometry for debugging purposes
                RAISE NOTICE 'Intersection is not a LineString. Skipping geometry: %', old_street.geom;
                continue;
            END IF;

            -- STEP 4: CALCULATE INTERSECTION POINT
            -- Find the exact intersection point between the buffered current way and the old street
            -- ST_LineInterpolatePoint(..., 0.5) gets the midpoint of the intersection line
            SELECT ST_LineInterpolatePoint(ST_Intersection(ST_Buffer(way.geom, 0.1), old_street.geom), 0.5) AS geom
            INTO interpolate_point;

            -- STEP 5: REMOVE ORIGINAL INTERSECTING STREET
            -- Delete the original old street since we'll replace it with two segments
            DELETE
            FROM ways_tem
            WHERE way_id = old_street.way_id;

            -- STEP 6: CREATE SPLIT SEGMENTS FOR OLD STREET
            -- Split the old street into two parts at the intersection point
            
            -- Insert first half of old street (from start to intersection point)
            -- ST_LineLocatePoint finds the position (0-1) of interpolate_point on old_street
            -- ST_LineSubstring(geom, 0, position) extracts line from start to that position
            PERFORM insert_way_segment(
                old_street.clazz,  -- Preserve original classification
                ST_LineSubstring(old_street.geom, 0, ST_LineLocatePoint(
                    old_street.geom, interpolate_point.geom))
            );
            
            -- Insert second half of old street (from intersection point to end)
            -- ST_LineSubstring(geom, position, 1) extracts line from position to end
            PERFORM insert_way_segment(
                old_street.clazz,  -- Preserve original classification
                ST_LineSubstring(old_street.geom, ST_LineLocatePoint(
                    old_street.geom, interpolate_point.geom), 1)
            );

            -- STEP 7: REMOVE ORIGINAL CURRENT WAY
            -- Delete the current way since we'll replace it with two segments
            DELETE FROM ways_tem WHERE way_id = way.way_id;

            -- STEP 8: CREATE SPLIT SEGMENTS FOR CURRENT WAY
            -- Split the current way into two parts at the intersection point
            
            -- Insert first half of current way (from start to intersection point)
            PERFORM insert_way_segment(
                way.clazz,  -- Preserve original classification
                ST_LineSubstring(way.geom, 0, ST_LineLocatePoint(
                    way.geom, interpolate_point.geom))
            );

            -- Insert second half of current way (from intersection point to end)
            PERFORM insert_way_segment(
                way.clazz,  -- Preserve original classification
                ST_LineSubstring(way.geom, ST_LineLocatePoint(
                    way.geom, interpolate_point.geom), 1)
            );

        end loop; -- End of main processing loop
    
    
    RAISE NOTICE 'Way connections have been successfully drawn and segmented.';
    
end;
$$;

