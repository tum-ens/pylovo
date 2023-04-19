--
-- PostgreSQL database dump
--

-- Dumped from database version 12.8
-- Dumped by pg_dump version 12.8

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: hstore; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS hstore WITH SCHEMA public;


--
-- Name: EXTENSION hstore; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION hstore IS 'data type for storing sets of (key, value) pairs';


--
-- Name: postgis; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS postgis WITH SCHEMA public;


--
-- Name: EXTENSION postgis; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION postgis IS 'PostGIS geometry and geography spatial types and functions';


--
-- Name: pgrouting; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pgrouting WITH SCHEMA public;


--
-- Name: EXTENSION pgrouting; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION pgrouting IS 'pgRouting Extension';


--
-- Name: categorize_loadareas(); Type: FUNCTION; Schema: public; Owner: postgres
--

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


ALTER FUNCTION public.categorize_loadareas() OWNER TO postgres;

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


ALTER FUNCTION public.draw_home_connections() OWNER TO postgres;

--
-- Name: draw_way_connections(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.draw_way_connections() RETURNS void
    LANGUAGE plpgsql
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
		
		SELECT 
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


ALTER FUNCTION public.draw_way_connections() OWNER TO postgres;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: betriebsmittel; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.betriebsmittel (
    name character varying(100) NOT NULL,
    s_max_kva integer,
    max_i_a integer,
    r_mohm_per_km integer,
    x_mohm_per_km integer,
    z_mohm_per_km integer,
    kosten_eur integer,
    typ character varying(50),
    anwendungsgebiet integer
);


ALTER TABLE public.betriebsmittel OWNER TO postgres;

--
-- Name: building_clusters; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.building_clusters (
    s_max bigint,
    model_status integer,
    cable integer,
    building_cluster bigint,
    loadarea_cluster bigint,
    k_mean_cluster integer,
    ont_vertice_id bigint
);


ALTER TABLE public.building_clusters OWNER TO postgres;

--
-- Name: buildings_result; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.buildings_result (
    osm_id character varying(80),
    area numeric,
    type character varying(30),
    geom public.geometry(Geometry,3035),
    houses_per_building integer,
    center public.geometry(Geometry,3035),
    in_loadarea integer,
    peak_load_in_kw numeric,
    in_loadarea_cluster integer,
    vertice_id integer,
    in_building_cluster integer,
    k_mean_cluster integer,
    floors integer,
    connection_point integer
);


ALTER TABLE public.buildings_result OWNER TO postgres;

--
-- Name: buildings_tem; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.buildings_tem (
    osm_id character varying(80),
    area numeric,
    type character varying(80),
    geom public.geometry(MultiPolygon),
    houses_per_building integer,
    center public.geometry(Geometry,3035),
    in_loadarea integer,
    peak_load_in_kw numeric,
    in_loadarea_cluster bigint,
    vertice_id bigint,
    in_building_cluster bigint,
    k_mean_cluster integer,
    floors integer,
    connection_point integer
);


ALTER TABLE public.buildings_tem OWNER TO postgres;

--
-- Name: consumer_categories; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.consumer_categories (
    id integer NOT NULL,
    definition character varying(30) NOT NULL,
    peak_load numeric(10,2),
    yearly_consumption numeric(10,2),
    peak_load_per_m2 numeric(10,2),
    yearly_consumption_per_m2 numeric(10,2),
    sim_factor numeric(10,2) NOT NULL
);


ALTER TABLE public.consumer_categories OWNER TO postgres;

--
-- Name: loadarea; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.loadarea (
    id integer,
    cluster_id integer,
    area_ha numeric,
    ags_0 character varying(255),
    zensus_sum integer,
    geom_centre public.geometry(Geometry,3035),
    geom public.geometry(Geometry,3035),
    hausabstand numeric,
    siedlungstyp integer
);


ALTER TABLE public.loadarea OWNER TO postgres;

--
-- Name: postcode; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.postcode (
    gid integer NOT NULL,
    plz character varying(5),
    note character varying(86),
    qkm double precision,
    einwohner integer,
    geom public.geometry(MultiPolygon)
);


ALTER TABLE public.postcode OWNER TO postgres;

--
-- Name: plz-5stellig_gid_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public."plz-5stellig_gid_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public."plz-5stellig_gid_seq" OWNER TO postgres;

--
-- Name: plz-5stellig_gid_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public."plz-5stellig_gid_seq" OWNED BY public.postcode.gid;


--
-- Name: postcode_result; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.postcode_result (
    id integer,
    siedlungstyp integer,
    geom public.geometry(Geometry,3035),
    hausabstand numeric
);


ALTER TABLE public.postcode_result OWNER TO postgres;

--
-- Name: transformer_positions; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.transformer_positions (
    loadarea_cluster integer,
    k_mean_cluster integer,
    building_cluster integer,
    geom public.geometry(Geometry,3035),
    ogc_fid character varying(50),
    comment character varying
);


ALTER TABLE public.transformer_positions OWNER TO postgres;

--
-- Name: ways; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.ways (
    clazz integer,
    source integer,
    target integer,
    cost double precision,
    reverse_cost double precision,
    geom public.geometry(Geometry,3035),
    id integer,
);


ALTER TABLE public.ways OWNER TO postgres;

--
-- Name: ways_result; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.ways_result (
    clazz integer,
    source integer,
    target integer,
    cost double precision,
    reverse_cost double precision,
    geom public.geometry(Geometry,3035),
    id integer
);


ALTER TABLE public.ways_result OWNER TO postgres;

--
-- Name: ways_tem; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.ways_tem (
    clazz integer,
    source integer,
    target integer,
    cost double precision,
    reverse_cost double precision,
    geom public.geometry(Geometry,3035),
    id integer
);


ALTER TABLE public.ways_tem OWNER TO postgres;

--
-- Name: postcode gid; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.postcode ALTER COLUMN gid SET DEFAULT nextval('public."plz-5stellig_gid_seq"'::regclass);


--
-- Name: betriebsmittel betriebsmittel_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.betriebsmittel
    ADD CONSTRAINT betriebsmittel_pkey PRIMARY KEY (name);


--
-- Name: consumer_categories consumer_categories_definition_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.consumer_categories
    ADD CONSTRAINT consumer_categories_definition_key UNIQUE (definition);


--
-- Name: consumer_categories consumer_categories_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.consumer_categories
    ADD CONSTRAINT consumer_categories_pkey PRIMARY KEY (id);


--
-- Name: postcode plz-5stellig_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.postcode
    ADD CONSTRAINT "plz-5stellig_pkey" PRIMARY KEY (gid);


--
-- Name: idx_public_2po_4pgr_source; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_public_2po_4pgr_source ON public.ways USING btree (source);


--
-- Name: idx_public_2po_4pgr_target; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_public_2po_4pgr_target ON public.ways USING btree (target);


--
-- Name: plz-5stellig_geom_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "plz-5stellig_geom_idx" ON public.postcode USING gist (geom);


--
-- Name: ways_tem_geom_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ways_tem_geom_idx ON public.ways_tem USING gist (geom);


--
-- Name: ways_tem_id_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ways_tem_id_idx ON public.ways_tem USING btree (id);


--
-- Name: ways_tem_source_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ways_tem_source_idx ON public.ways_tem USING btree (source);


--
-- Name: ways_tem_target_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ways_tem_target_idx ON public.ways_tem USING btree (target);


--
-- PostgreSQL database dump complete
--

