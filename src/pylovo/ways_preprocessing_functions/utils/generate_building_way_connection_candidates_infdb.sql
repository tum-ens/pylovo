/*
 * FUNCTION PURPOSE AND OVERVIEW:
 * =============================
 * This function analyzes buildings with electrical load requirements and identifies
 * the optimal connection points to the existing ways network using infdb data.
 * It creates a temporary table containing precomputed connection candidates that can be used
 * for infrastructure planning and ways network extension.
 *
 * The function performs spatial analysis with address-aware matching:
 * 1. Identify buildings that require way connections (non-zero peak load)
 * 2. First attempt to match buildings to ways using address_street_id information
 * 3. Fall back to nearest suitable way if no address match is found
 * 4. Calculate the optimal connection line from building to way
 * 5. Determine the precise connection point on the way geometry
 * 6. Store results in an indexed temporary table for efficient processing
 *
 * ALGORITHM OVERVIEW:
 * 1. Filter buildings that need connections (peak_load_in_kw != 0)
 * 2. For each building, first try to match using address_street_id
 * 3. If no address match, find the closest suitable way within 2000 units
 * 4. Generate shortest connection line from building centroid to way
 * 5. Calculate the exact connection point on the way geometry
 * 6. Create indexed temporary table for efficient downstream processing
 *
 * INPUT: Data from 'buildings_tem' and 'ways_tem' tables (INFDB database)
 * OUTPUT: Temporary table 'temp_building_connection_candidates_infdb' with connection analysis
 */

CREATE OR REPLACE FUNCTION generate_building_way_connection_candidates_infdb() RETURNS void AS $$
BEGIN
    -- MAIN PROCESSING: Create temporary table with comprehensive connection analysis
    -- This table will contain all necessary data for building-to-way connections
    DROP TABLE IF EXISTS temp_building_connection_candidates_infdb;
    CREATE TEMP TABLE temp_building_connection_candidates_infdb AS
    
    -- CTE 1: FILTER BUILDINGS REQUIRING CONNECTIONS
    -- Extract only buildings that have electrical load requirements and address information
    -- These are the buildings that need to be connected to the way network
    WITH buildings AS (
        SELECT 
            objectid,              -- Unique building identifier
            centroid,              -- Geometric centroid point of the building
            address_street_id    -- Street ID from address data
        FROM buildings_tem
        WHERE peak_load_in_kw <> 0  -- Only buildings with non-zero electrical load
                                    -- These represent active buildings needing connections
    ),

    -- CTE 2: FIND BEST SUITABLE WAY FOR EACH BUILDING
    -- First try address-based match, then fall back to nearest way search
    closest_road AS (
        SELECT 
            b.objectid,                                                              -- Building identifier
            b.centroid,                                                              -- Building centroid point
            COALESCE(direct_way.way_id, nearest_way.way_id) AS old_way_id,        -- ID of the matched way (address-based or nearest)
            COALESCE(direct_way.geom, nearest_way.geom) AS old_geom,              -- Geometry of the matched way
            ST_ShortestLine(b.centroid, COALESCE(direct_way.geom, nearest_way.geom)) AS new_geom  -- Shortest connection line from building to way
        FROM buildings b
        
        -- FIRST PRIORITY: Try to find direct match using address_street_id
        LEFT JOIN ways_tem direct_way ON (
            b.address_street_id IS NOT NULL                    -- Building must have address information
            AND direct_way.way_id = b.address_street_id       -- Direct match with address street ID
            AND direct_way.clazz != 110                       -- Exclude ways which are connection lines (from buildings to ways)
        )
        
        -- FALLBACK: If no direct address match, find nearest way using spatial analysis
        -- LATERAL JOIN allows us to use building-specific filters in the subquery
        LEFT JOIN LATERAL (
            SELECT way_id, geom
            FROM ways_tem w
            WHERE w.clazz != 110                              -- Exclude ways which are connection lines (from buildings to ways)
              AND ST_DWithin(b.centroid, w.geom, 2000)         -- Limit search to 2000 units radius for performance
              AND ST_Distance(b.centroid, w.geom) > 0.1        -- Exclude ways that are too close (avoid geometric errors)
            ORDER BY b.centroid <-> w.geom                      -- Sort by distance using KNN operator (<->)
            LIMIT 1                                           -- Take only the closest way
        ) nearest_way ON (direct_way.way_id IS NULL)          -- Only execute if no direct match found
        
        -- Ensure we have at least one valid way (either from address match or proximity search)
        WHERE COALESCE(direct_way.geom, nearest_way.geom) IS NOT NULL
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
        FROM closest_road c
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
    CREATE INDEX temp_candidates_old_way_idx ON temp_building_connection_candidates_infdb (old_way_id);
    
    -- Spatial index on connection_point for fast geometric searches
    -- Enables efficient spatial queries and nearest-neighbor operations
    CREATE INDEX temp_candidates_connection_gix ON temp_building_connection_candidates_infdb USING GIST (connection_point);
    
    -- Note: These indexes significantly improve performance when the temporary table
    -- is used in subsequent spatial operations or way segmentation processes
    
END;
$$ LANGUAGE plpgsql;