/*
 * FUNCTION PURPOSE AND OVERVIEW:
 * =============================
 * This function splits a single linear geometry of ways into multiple
 * segments at specified point locations. It takes a line geometry and an array of
 * point geometries, then returns each segment between consecutive points as separate
 * geometric objects.
 *
 * The function is essential for network segmentation operations where you need to
 * divide continuous linear features at specific intersection or connection points.
 *
 * ALGORITHM OVERVIEW:
 * 1. Convert each point to a fractional position along the input line (0.0 to 1.0)
 * 2. Iterate through points in array order
 * 3. Extract line segment from previous point to current point
 * 4. Return each valid segment as a separate geometry
 * 5. Handle the final segment from last point to line end
 *
 * INPUT PARAMETERS:
 * - line: LineString geometry to be split
 * - points: Array of Point geometries indicating where to split the line
 *
 * OUTPUT:
 * - TABLE with 'part' column containing LineString geometries for each segment
 * - Each row represents one segment between consecutive split points
 */

CREATE OR REPLACE FUNCTION split_way_at_connection_points(line geometry, points geometry[])
RETURNS TABLE(part geometry) AS $$
DECLARE
    -- Loop counter for iterating through the points array
    i INTEGER;

    -- Fractional position along the line where the previous segment ended
    -- Ranges from 0.0 (line start) to 1.0 (line end)
    start_fraction FLOAT := 0;

    -- Fractional position along the line where the current segment should end
    end_fraction FLOAT;

BEGIN
    -- MAIN PROCESSING LOOP: Iterate through all points in the input array
    -- Each iteration creates one line segment between consecutive points
    FOR i IN 1 .. array_length(points, 1)
    LOOP
        -- STEP 1: Calculate fractional position of current point along the line
        -- ST_LineLocatePoint returns a value between 0.0 (start) and 1.0 (end)
        -- indicating where this point lies along the line geometry
        end_fraction := ST_LineLocatePoint(line, points[i]);

        -- STEP 2: Validate segment and extract geometry
        -- Only create a segment if the end point is further along than the start
        -- This prevents zero-length or backwards segments
        IF end_fraction > start_fraction THEN
            -- Extract the line segment between start_fraction and end_fraction
            -- ST_LineSubstring creates a new LineString from the specified portion
            RETURN QUERY
            SELECT ST_LineSubstring(line, start_fraction, end_fraction);
        END IF;

        -- STEP 3: Update start position for next iteration
        -- The end of this segment becomes the start of the next segment
        start_fraction := end_fraction;

    END LOOP;

    -- STEP 4: HANDLE FINAL SEGMENT
    -- Create the last segment from the final split point to the end of the line
    -- This ensures the entire original line is covered by the returned segments
    IF start_fraction < 1 THEN
        -- Extract segment from last split point to the end of the line (fraction 1.0)
        RETURN QUERY
        SELECT ST_LineSubstring(line, start_fraction, 1);
    END IF;

END;
$$ LANGUAGE plpgsql;
