CREATE FUNCTION public.categorize_loadareas() RETURNS void
    LANGUAGE plpgsql
    AS $$
declare
	cls integer; -- cluster_id
	avg_distance float; -- avg. distance per cluster
begin
	--iterate each loadarea cluster
	for cls in
		SELECT DISTINCT cluster_id
		FROM public.loadarea
		WHERE cluster_id <> -1
		order by cluster_id
	loop
		WITH some_buildings AS (
			SELECT osm_id, center FROM public.buildings TABLESAMPLE SYSTEM(5)
			WHERE in_loadarea_cluster = cls)

		, closest_distances AS(
		SELECT s.osm_id, c.dist as dist
		FROM some_buildings as s
		LEFT JOIN LATERAL(
			SELECT ST_Distance(s.center, b.center) as dist
			FROM public.buildings as b
			WHERE ST_DWithin(s.center, b.center, 1000)
			AND s.osm_id <> b.osm_id
			ORDER BY s.center <-> b.center
			LIMIT 10) as c
			ON TRUE)


		SELECT AVG(dist)
		INTO avg_distance
		FROM closest_distances;

		UPDATE public.loadarea
		SET hausabstand = avg_distance
		WHERE cluster_id = cls;

		RAISE NOTICE 'avg. distance: %', avg_distance;
		RAISE NOTICE 'cluster: %', cls;
	end loop;


end;
$$;



--
-- Name: draw_home_connections(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.draw_home_connections() RETURNS void
    LANGUAGE plpgsql
    AS $$
declare
	building RECORD;
	new_line RECORD;
	old_street RECORD;
begin
	-- Iterating buildings
	for building in
		SELECT osm_id, center
		FROM public.buildings_tem
		WHERE peak_load_in_kw <> 0
	loop
		--Finde Hausanschluss -> new_line
		SELECT
			building.osm_id as id,
			110 as clazz,
			a.line as geom
		INTO new_line FROM
			(SELECT ST_ShortestLine(building.center, w.geom) as line
			 FROM public.ways_tem as w
			 WHERE w.clazz != 110 -- Hausanschlüsse und alte Straßen ausgeschlossen
			 	AND ST_DWithin(building.center, w.geom, 2000)
			 	AND ST_Distance(building.center, w.geom) > 0.1
			 ORDER BY building.center <-> w.geom
			 LIMIT 1) as a;

		IF NOT FOUND THEN
			continue;
		END IF;
		-- Finde die angeschlossene Straße -> old_street
		SELECT *,
		ST_LineInterpolatePoint(ST_Intersection(
			ST_Buffer(new_line.geom,0.1), w.geom), 0.5) as connection_point
		INTO old_street
		FROM public.ways_tem as w
		WHERE ST_DWithin(new_line.geom, w.geom, 1000) -- begrenzen
			AND ST_Intersects(ST_Buffer(new_line.geom,0.001), w.geom) -- Kontakt existiert
			AND w.clazz != 110
			AND ST_GeometryType(ST_Intersection(
				ST_Buffer(new_line.geom,0.1), w.geom)) = 'ST_LineString'
		ORDER BY new_line.geom <-> w.geom
		LIMIT 1; -- zur Garantie gegen mehrere Kontakte

		IF NOT FOUND THEN
			continue;
		END IF;
		-- Überprüfe ob das Lot (ShortestLine) nah zu einem Knoten ist, wenn ja verschiebe
		IF ST_Distance(ST_StartPoint(old_street.geom), old_street.connection_point) < 0.1 THEN

			new_line.geom := ST_Makeline(building.center, ST_StartPoint(old_street.geom));
			INSERT INTO public.ways_tem (id, clazz, geom)
			SELECT
				Max(id)+1,
				new_line.clazz,
				new_line.geom
			FROM public.ways_tem;
			--raise notice '<0.01';
			continue;
		ELSEIF ST_Distance(ST_EndPoint(old_street.geom), old_street.connection_point) < 0.1 THEN

			new_line.geom := ST_Makeline(building.center, ST_EndPoint(old_street.geom));
			INSERT INTO public.ways_tem (id, clazz, geom)
			SELECT
				Max(id)+1,
				new_line.clazz,
				new_line.geom
			FROM public.ways_tem;
			--raise notice '>0.99';
			continue;
		ELSE
			INSERT INTO public.ways_tem (id, clazz, geom)
			SELECT
				Max(id)+1,
				new_line.clazz,
				new_line.geom
			FROM public.ways_tem;
			--raise notice 'normal';
		END IF;

		--Hausanschluss schneidet die Straße
		--old_street clazz ungültig machen
		DELETE FROM public.ways_tem
			WHERE id = old_street.id;

		-- INSERT new streets as two part of old street
		-- 	first half
		INSERT INTO public.ways_tem (id, clazz, geom)
		SELECT Max(id)+1, --unique id guarenteed
				103,
				ST_LineSubstring(old_street.geom, 0, ST_LineLocatePoint(
					old_street.geom, old_street.connection_point))
		FROM public.ways_tem;
		--  second half
		INSERT INTO public.ways_tem (id, clazz, geom)
		SELECT Max(id)+1,
				103,
				ST_LineSubstring(old_street.geom, ST_LineLocatePoint(
					old_street.geom, old_street.connection_point), 1)
		FROM public.ways_tem;


	end loop;
	raise notice 'Home connections are drawn successfully...';
end;
$$;



--
-- Name: draw_way_connections(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.draw_way_connections() RETURNS void
    LANGUAGE PLPGSQL
    AS $$
declare
	way RECORD;
	interpolate_point RECORD;
	old_street RECORD;
begin
	-- Iterating buildings
	for way in
		SELECT geom, clazz, id
		FROM public.ways_tem
	loop
		--Finde Hausanschluss -> new_line
		SELECT geom, clazz, id
		INTO old_street
		FROM public.ways_tem as w
		WHERE ST_Intersects(ST_LineSubstring(way.geom, 0.01, 0.99), w.geom) -- begrenzen
			AND w.id != way.id
		LIMIT 1;

		IF NOT FOUND THEN
			continue;
		END IF;

		-- check whether the intersection of ST_Buffer(way.geom,0.1) , old_street.geom is a line
		-- this is necessary for the next SELECT statement with ST_LineInterpolatePoint()-function
-- 		IF ST_Intersection(ST_Buffer(way.geom,0.1) , old_street.geom) != "ST_LineString" THEN
-- 		    continue;
-- 		END IF;

		SELECT
			-- ST_LineInterpolatePoint(ST_LineMerge(ST_Intersection(ST_Buffer(way.geom,0.1) , old_street.geom)),0.5) AS geom
			ST_LineInterpolatePoint(ST_Intersection(ST_Buffer(way.geom,0.1) , old_street.geom),0.5) AS geom
		INTO interpolate_point;

		DELETE FROM public.ways_tem
			WHERE id = old_street.id;

		-- INSERT new streets as two part of old street
		-- 	first half
		INSERT INTO public.ways_tem (id, clazz, geom)
		SELECT Max(id)+1, --unique id guarenteed
				old_street.clazz,
				ST_LineSubstring(old_street.geom, 0, ST_LineLocatePoint(
					old_street.geom, interpolate_point.geom))
		FROM public.ways_tem;
		--  second half
		INSERT INTO public.ways_tem (id, clazz, geom)
		SELECT Max(id)+1,
				old_street.clazz,
				ST_LineSubstring(old_street.geom, ST_LineLocatePoint(
					old_street.geom, interpolate_point.geom), 1)
		FROM public.ways_tem;

		DELETE FROM public.ways_tem
			WHERE id = way.id;

		INSERT INTO public.ways_tem (id, clazz, geom)
		SELECT Max(id)+1, --unique id guarenteed
				way.clazz,
				ST_LineSubstring(way.geom, 0, ST_LineLocatePoint(
					way.geom, interpolate_point.geom))
		FROM public.ways_tem;
		--  second half
		INSERT INTO public.ways_tem (id, clazz, geom)
		SELECT Max(id)+1,
				way.clazz,
				ST_LineSubstring(way.geom, ST_LineLocatePoint(
					way.geom, interpolate_point.geom), 1)
		FROM public.ways_tem;

	end loop;
	raise notice 'Home connections are drawn successfully...';
end;
$$;