/*
 * FUNCTION PURPOSE AND OVERVIEW:
 * =============================
 * This function analyzes buildings with electrical load requirements and identifies
 * the optimal connection points to the existing ways network. It creates
 * a temporary table containing precomputed connection candidates
 *
 * The function performs spatial analysis to:
 * 1. Identify buildings that require ways connections (non-zero peak load)
 * 2. Find the nearest suitable way for each building
 * 3. Calculate the optimal connection line from building to way
 * 4. Determine the precise connection point on the way geometry
 * 5. Store results in an indexed temporary table for efficient processing
 *
 * ALGORITHM OVERVIEW:
 * 1. Filter buildings that need connections (peak_load_in_kw != 0)
 * 2. For each building, find the closest suitable way within 2000 units
 * 3. Generate shortest connection line from building centroid to way
 * 4. Calculate the exact connection point on the way geometry
 * 5. Create indexed temporary table for efficient downstream processing
 *
 * INPUT: Data from 'buildings_tem' and 'ways_tem' tables
 * OUTPUT: Temporary table 'temp_building_connection_candidates' with connection analysis
 */

CREATE OR REPLACE FUNCTION generate_building_way_connection_candidates() RETURNS void AS $$
BEGIN
    -- MAIN PROCESSING: Create temporary table with comprehensive connection analysis
    -- This table will contain all necessary data for building-to-way connections
    DROP TABLE IF EXISTS temp_building_connection_candidates;
    CREATE TEMP TABLE temp_building_connection_candidates AS
    
    -- CTE 1: FILTER BUILDINGS REQUIRING CONNECTIONS
    -- Extract only buildings that have electrical load requirements
    -- These are the buildings that need to be connected to the ways network
    WITH buildings AS (
        SELECT 
            objectid,           -- Unique building identifier
            centroid            -- Geometric centroid point of the building
        FROM buildings_tem
        WHERE peak_load_in_kw <> 0  -- Only buildings with non-zero electrical load
    ),

    -- CTE 2: FIND CLOSEST SUITABLE way FOR EACH BUILDING
    -- For each building, identify the nearest way that can serve as connection point
    closest_way AS (
        SELECT 
            b.objectid,                                      -- Building identifier
            b.centroid,                                      -- Building centroid point
            w.way_id AS old_way_id,                       -- ID of the closest way
            w.geom AS old_geom,                           -- Geometry of the closest way
            ST_ShortestLine(b.centroid, w.geom) AS new_geom -- Shortest connection line from building to way
        FROM buildings b
        -- LATERAL JOIN: For each building, find the single closest way
        -- This allows us to use building-specific filters in the subquery
        JOIN LATERAL (
            SELECT way_id, geom
            FROM ways_tem w
            WHERE w.clazz != 110                          -- Exclude ways which are connection lines (from buildings to ways)
              AND ST_DWithin(b.centroid, w.geom, 2000)     -- Limit search to 2000 units radius
              AND ST_Distance(b.centroid, w.geom) > 0.1    -- Exclude ways that are too close (avoid geometric errors)
            ORDER BY b.centroid <-> w.geom                  -- Sort by distance using KNN operator (<->)
            LIMIT 1                                       -- Take only the closest way
        ) w ON TRUE                                       -- LATERAL join condition
        WHERE w.geom IS NOT NULL                          -- Ensure we found a valid way
    ),

    -- CTE 3: CALCULATE PRECISE CONNECTION POINTS
    -- Determine the exact point on each way where the building should connect
    connection_line AS (
        SELECT 
            c.objectid,                                           -- Building identifier
            c.centroid,                                           -- Building centroid point
            c.old_way_id,                                       -- Connected way ID
            c.old_geom,                                         -- Connected way geometry
            c.new_geom,                                         -- Connection line geometry
            ST_ClosestPoint(c.old_geom, c.new_geom) AS connection_point  -- Exact point on way for connection
                                                                          -- This is where the way will be split
        FROM closest_way c
    )

    -- FINAL SELECT: Create the results table with deduplication
    -- Handle cases where multiple buildings might connect to the same point
    SELECT DISTINCT ON (objectid)                    -- Ensure one record per building
        objectid,              -- Building identifier
        centroid,              -- Building centroid point
        new_geom,            -- Connection line from building to way
        old_way_id,          -- ID of way that will be connected to
        old_geom,            -- Geometry of way that will be connected to
        connection_point     -- Exact point on way where connection will be made
    FROM connection_line;
    
    -- Index on old_way_id for fast grouping of connections by way
    -- Useful when processing multiple connections to the same way segment
    CREATE INDEX temp_candidates_old_way_idx ON temp_building_connection_candidates (old_way_id);
    
    -- Spatial index on connection_point for fast geometric searches
    -- Enables efficient spatial queries and nearest-neighbor operations
    CREATE INDEX temp_candidates_connection_gix ON temp_building_connection_candidates USING GIST (connection_point);

    
END;
$$ LANGUAGE plpgsql;