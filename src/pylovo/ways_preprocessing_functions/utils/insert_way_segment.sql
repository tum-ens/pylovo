/*
 * FUNCTION PURPOSE AND OVERVIEW:
 * =============================
 * This function adds a new way record to the ways_tem table
 * with an automatically generated unique identifier. It's a utility function designed
 * to simplify the insertion of new way geometries while ensuring proper ID management.
 * 
 * The function automatically calculates the next available way_id by finding the current
 * maximum ID in the table and incrementing it by 1, ensuring no ID conflicts occur.
 *
 * INPUT PARAMETERS:
 * - p_clazz: Integer classification/type code for the way
 * - p_geom: PostGIS geometry object representing the way's spatial data
 *
 * OUTPUT: 
 * - Inserts one new record into ways_tem table
 * - Returns void (no return value)
 */

CREATE OR REPLACE FUNCTION insert_way_segment(p_clazz int, p_geom geometry)
RETURNS void AS $$
BEGIN
    -- Insert new way record into the temporary ways table
    -- Automatically generate the next sequential way_id to avoid conflicts
    INSERT INTO ways_tem (
        way_id,    -- Auto-generated unique identifier
        clazz,     -- Way classification
        geom       -- Geometric representation of the way
    )
    SELECT 
        MAX(way_id) + 1,  -- Find current highest ID and increment by 1
        p_clazz,          -- Use provided classification parameter
        p_geom            -- Use provided geometry parameter
    FROM ways_tem;        -- Source table for finding maximum ID
    
    
END;
$$ LANGUAGE plpgsql;
