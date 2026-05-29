/*
 * FUNCTION PURPOSE AND OVERVIEW:
 * =============================
 * This function is the main orchestrator for creating building-to-way connections.
 * It performs a complete end-to-end process of analyzing buildings, identifying connection
 * points, creating connection lines, and segmenting the existing way network to accommodate
 * new connections.
 *
 * The function transforms a continuous way network into a segmented network with proper
 * building connections
 *
 * ALGORITHM OVERVIEW:
 * The function operates in four distinct phases:
 * 1. ANALYSIS PHASE: Generate connection candidates using spatial analysis
 * 2. CONNECTION PHASE: Create connection lines, handling endpoint cases
 * 3. GROUPING PHASE: Organize connection points by affected ways
 * 4. SEGMENTATION PHASE: Split ways at connection points and rebuild network
 *
 * WORKFLOW INTEGRATION:
 * This function integrates with:
 * - generate_building_way_connection_candidates(): For spatial analysis
 * - insert_way_segment(): For adding new geometric segments
 * - split_way_at_connection_points(): For way segmentation
 *
 * INPUT: Data from buildings_tem and ways_tem tables
 * OUTPUT: Modified ways_tem table with connection lines and segmented ways
 *
 * CLASSIFICATION CODES USED:
 * - 110: Building connection lines (service connections)
 * - 103: Segmented way pieces (original way infrastructure)
 */

CREATE OR REPLACE FUNCTION generate_building_to_way_connections() RETURNS void
LANGUAGE plpgsql AS
$$
DECLARE
    -- Record for iterating through building connection candidates
    r RECORD;
    
    -- Record for iterating through grouped way splits
    s RECORD;
    
    -- Temporary geometry variable for building connection lines
    final_geom geometry;
    
    -- Individual way segment returned from splitting operations
    part geometry;
    
BEGIN
    -- =====================================================================================
    -- PHASE 1: ANALYSIS - Generate Connection Candidates
    -- =====================================================================================
    -- Generate analysis of building-to-way connections
    -- This creates temp_building_connection_candidates table with optimal connection points
    PERFORM generate_building_way_connection_candidates();

    -- =====================================================================================
    -- PHASE 2: CONNECTION CREATION - Process Individual Building Connections
    -- =====================================================================================
    -- Iterate through each building that needs a way connection
    -- Handle special cases where connections are near way endpoints
    FOR r IN SELECT * FROM temp_building_connection_candidates
    LOOP
        -- CASE 1: CONNECTION NEAR WAY START POINT
        -- If connection point is very close to the start of the existing way
        -- Connect directly to the start point to avoid unnecessary way splitting
        IF ST_Distance(ST_StartPoint(r.old_geom), r.connection_point) < 0.1 THEN
            -- Create connection line from building centroid to way start point
            final_geom := ST_MakeLine(r.centroid, ST_StartPoint(r.old_geom));
            
            -- Insert connection line with classification 110 (building connection)
            PERFORM insert_way_segment(110, final_geom);

            -- Remove this building from candidates since no way splitting is needed
            -- The connection uses the existing way endpoint
            DELETE FROM temp_building_connection_candidates
            WHERE objectid = r.objectid;
            
        -- CASE 2: CONNECTION NEAR WAY END POINT  
        -- If connection point is very close to the end of the existing way
        -- Connect directly to the end point to avoid unnecessary way splitting
        ELSIF ST_Distance(ST_EndPoint(r.old_geom), r.connection_point) < 0.1 THEN
            -- Create connection line from building centroid to way end point
            final_geom := ST_MakeLine(r.centroid, ST_EndPoint(r.old_geom));
            
            -- Insert connection line with classification 110 (building connection)
            PERFORM insert_way_segment(110, final_geom);

            -- Remove this building from candidates since no way splitting is needed
            -- The connection uses the existing way endpoint
            DELETE FROM temp_building_connection_candidates
            WHERE objectid = r.objectid;

        -- CASE 3: CONNECTION IN MIDDLE OF WAY
        -- Connection point requires splitting the existing way
        -- Use the pre-calculated optimal connection line
        ELSE
            -- Use the connection line calculated during analysis phase
            final_geom := r.new_geom;
            
            -- Insert connection line with classification 110 (building connection)
            PERFORM insert_way_segment(110, final_geom);
        END IF;
    END LOOP;

    -- =====================================================================================
    -- PHASE 3: GROUPING - Organize Connection Points by Affected Ways
    -- =====================================================================================
    -- Group remaining connection points by the ways they will split
    -- This ensures each way is split only once, even with multiple connections
    DROP TABLE IF EXISTS grouped_splits;
    CREATE TEMP TABLE grouped_splits AS
    SELECT 
        old_way_id,                           -- ID of way that will be split
        old_geom,                            -- Geometry of way that will be split  
        -- Aggregate all connection points for this way, ordered by position along the way
        -- This ordering is crucial for proper segmentation
        ARRAY_AGG(connection_point ORDER BY ST_LineLocatePoint(old_geom, connection_point)) AS connection_points
    FROM temp_building_connection_candidates  -- Only remaining candidates need way splitting
    GROUP BY old_way_id, old_geom;           -- One group per unique way

    -- =====================================================================================
    -- PHASE 4: SEGMENTATION - Split Ways and Rebuild Network
    -- =====================================================================================
    -- Process each way that needs to be split for building connections
    FOR s IN SELECT * FROM grouped_splits LOOP
        -- STEP 1: Remove original continuous way from the network
        -- This way will be replaced by multiple segments with connection points
        DELETE FROM ways_tem WHERE way_id = s.old_way_id;

        -- STEP 2: Split the way at all connection points and insert segments
        -- split_way_at_connection_points returns multiple segments between connection points
        FOR part IN SELECT * FROM split_way_at_connection_points(s.old_geom, s.connection_points)
        LOOP
            -- Insert each way segment with classification 103 (segmented infrastructure)
            -- These represent the original way infrastructure, now properly segmented
            PERFORM insert_way_segment(103, part);
        END LOOP;
    END LOOP;

    -- =====================================================================================
    -- COMPLETION
    -- =====================================================================================
    -- At this point:
    -- 1. All buildings have connection lines (classification 110)
    -- 2. Original ways are segmented at connection points (classification 103)  
    -- 3. Network topology properly represents building-to-infrastructure connections

END;
$$;