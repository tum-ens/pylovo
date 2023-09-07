import os

# Database connection configuration
DBNAME = "pylovo_db"
USER = "pylovo"
PASSWORD = "pylovo"
HOST = "localhost"
PORT = "5432"
HOST = "10.162.28.8"

# DBNAME = "syngrid_db"
# USER = "syngrid"
# HOST = "localhost"
# PORT = "1111"
# PASSWORD = "syngrid"

# Directory where the result csv and json files be saved
RESULT_DIR = f"{os.getcwd()}\\results"

# Raw data
OGR_FILE_LIST = [
    {"path": ".\\raw_data\\Res_9174115\\Res_9174115.shp", "table_name": "res"},
    {"path": ".\\raw_data\\Oth_9174115\\Oth_9174115.shp", "table_name": "oth"}
]

# OGR_FILE_LIST = [
#     # Neufahrn bei Freising
#     {"path": ".\\raw_data\\Res_9178145\\Res_9178145.shp","table_name": "res",},
#     {"path": ".\\raw_data\\Oth_9178145\\Oth_9178145.shp", "table_name": "oth"},
#     # Garching
#     {"path": ".\\raw_data\\Res_9184119\\Res_9184119.shp","table_name": "res",},
#     {"path": ".\\raw_data\\Oth_9184119\\Oth_9184119.shp", "table_name": "oth"},
# ]

# OGR_FILE_LIST = [
    # München
    # {"path": ".\\raw_data\\Res_9162000\\Res_9162000.shp","table_name": "res",},
    # {"path": ".\\raw_data\\Oth_9162000\\Oth_9162000.shp", "table_name": "oth"},
    # {"path": ".\\raw_data\\substation_munich.geojson", "table_name": "transformers"},
    # # Forchheim
    # {"path": ".\\raw_data\\Res_9474126\\Res_9474126.shp","table_name": "res",},
    # {"path": ".\\raw_data\\Oth_9474126\\Oth_9474126.shp", "table_name": "oth"},
    # Dachau
    # {"path": ".\\raw_data\\Res_9174115\\Res_9174115.shp", "table_name": "res", },
    # {"path": ".\\raw_data\\Oth_9174115\\Oth_9174115.shp", "table_name": "oth"},
    # Bergkirchen
    # {"path": ".\\raw_data\\Res_9174113\\Res_9174113.shp", "table_name": "res", },
    # {"path": ".\\raw_data\\Oth_9174113\\Oth_9174113.shp", "table_name": "oth"},
    # Ingolstadt
    # {"path": ".\\raw_data\\Res_9161000\\Res_9161000.shp", "table_name": "res", },
    # {"path": ".\\raw_data\\Oth_9161000\\Oth_9161000.shp", "table_name": "oth"},
# ]

CSV_FILE_LIST = [
    {"path": ".\\raw_data\\betriebsmittel.csv", "table_name": "betriebsmittel"},
    {"path": ".\\raw_data\\postcode.csv", "table_name": "postcode"},
    # {"path": ".\\raw_data\\ways.csv", "table_name": "ways"},
]

# Database schema - table structure
CREATE_QUERIES = {
    "betriebsmittel": """CREATE TABLE IF NOT EXISTS public.betriebsmittel
(
    name character varying(100) COLLATE pg_catalog."default" NOT NULL,
    s_max_kva integer,
    max_i_a integer,
    r_mohm_per_km integer,
    x_mohm_per_km integer,
    z_mohm_per_km integer,
    kosten_eur integer,
    typ character varying(50) COLLATE pg_catalog."default",
    anwendungsgebiet integer,
    CONSTRAINT betriebsmittel_pkey PRIMARY KEY (name)
)""",
    "building_clusters": """CREATE TABLE IF NOT EXISTS public.building_clusters
(   version_id character varying(10) NOT NULL, 
    k_mean_cluster integer NOT NULL,
    building_cluster bigint NOT NULL,
    loadarea_cluster bigint NOT NULL,
    s_max bigint,
    model_status integer,
    cable integer,
    ont_vertice_id bigint,
    CONSTRAINT building_clusters_pkey PRIMARY KEY (version_id, k_mean_cluster, building_cluster, loadarea_cluster)
)""",
    "buildings_result": """
CREATE TABLE IF NOT EXISTS public.buildings_result
(   version_id character varying(10) NOT NULL, 
    osm_id character varying(80) COLLATE pg_catalog."default" NOT NULL,
    area numeric,
    type character varying(30) COLLATE pg_catalog."default",
    geom geometry(Geometry,3035),
    houses_per_building integer,
    center geometry(Geometry,3035),
    in_loadarea integer,
    peak_load_in_kw numeric,
    in_loadarea_cluster integer,
    vertice_id integer,
    in_building_cluster integer,
    k_mean_cluster integer,
    floors integer,
    connection_point integer
    ,
    CONSTRAINT buildings_result_pkey PRIMARY KEY (version_id, osm_id)
)""",
    #     "buildings_result.center": """CREATE TABLE IF NOT EXISTS public."buildings_result.center"
    # (
    #     id integer NOT NULL DEFAULT nextval('"public"."buildings_result.center_id_seq"'::regclass),
    #     geom geometry(Point,3035),
    #     osm_id character varying(80) COLLATE pg_catalog."default",
    #     area double precision,
    #     type character varying(30) COLLATE pg_catalog."default",
    #     houses_per_building integer,
    #     in_loadarea integer,
    #     peak_load_in_kw double precision,
    #     in_loadarea_cluster integer,
    #     vertice_id integer,
    #     in_building_cluster integer,
    #     k_mean_cluster integer,
    #     floors integer,
    #     connection_point integer,
    #     CONSTRAINT "buildings_result.center_pkey" PRIMARY KEY (id)
    # )""",
    "buildings_tem": """CREATE TABLE IF NOT EXISTS public.buildings_tem
(
    osm_id character varying(80) COLLATE pg_catalog."default",
    area numeric,
    type character varying(80) COLLATE pg_catalog."default",
    geom geometry(MultiPolygon),
    houses_per_building integer,
    center geometry(Geometry,3035),
    in_loadarea integer,
    peak_load_in_kw numeric,
    in_loadarea_cluster bigint,
    vertice_id bigint,
    in_building_cluster bigint,
    k_mean_cluster integer,
    floors integer,
    connection_point integer
)""",
    "consumer_categories": """CREATE TABLE IF NOT EXISTS public.consumer_categories
(
    id integer NOT NULL,
    definition character varying(30) COLLATE pg_catalog."default" NOT NULL,
    peak_load numeric(10,2),
    yearly_consumption numeric(10,2),
    peak_load_per_m2 numeric(10,2),
    yearly_consumption_per_m2 numeric(10,2),
    sim_factor numeric(10,2) NOT NULL,
    CONSTRAINT consumer_categories_pkey PRIMARY KEY (id),
    CONSTRAINT consumer_categories_definition_key UNIQUE (definition)
)""",
    "loadarea": """CREATE TABLE IF NOT EXISTS public.loadarea
(
    id integer,
    cluster_id integer,
    area_ha numeric,
    ags_0 character varying(255) COLLATE pg_catalog."default",
    zensus_sum integer,
    geom_centre geometry(Geometry,3035),
    geom geometry(Geometry,3035),
    hausabstand numeric,
    siedlungstyp integer
)""",
    "postcode": """CREATE TABLE IF NOT EXISTS public.postcode
(
    gid integer NOT NULL,
    plz character varying(5) COLLATE pg_catalog."default",
    note character varying(86) COLLATE pg_catalog."default",
    qkm double precision,
    einwohner integer,
    geom geometry(MultiPolygon,3035),
    CONSTRAINT "plz-5stellig_pkey" PRIMARY KEY (gid)
)""",
    "postcode_result": """CREATE TABLE IF NOT EXISTS public.postcode_result
(   version_id character varying(10) NOT NULL, 
    id integer NOT NULL,
    siedlungstyp integer,
    geom geometry(MultiPolygon,3035),
    hausabstand numeric,
    CONSTRAINT "postcode_result_pkey" PRIMARY KEY (version_id, id)
)""",
    "transformer_positions": """CREATE TABLE IF NOT EXISTS public.transformer_positions 
                    (   version_id character varying(10) NOT NULL, 
                        loadarea_cluster integer,
                        k_mean_cluster integer,
                        building_cluster integer,
                        geom geometry(Geometry,3035),
                        ogc_fid character varying(50),
                        "comment" character varying
                        )
                        """,
    #     "transformers": """CREATE TABLE IF NOT EXISTS public.transformers
    # (
    #     ogc_fid integer NOT NULL DEFAULT nextval('transformers_ogc_fid_seq'::regclass),
    #     id character varying COLLATE pg_catalog."default",
    #     "@id" character varying COLLATE pg_catalog."default",
    #     access character varying COLLATE pg_catalog."default",
    #     "addr:city" character varying COLLATE pg_catalog."default",
    #     "addr:country" character varying COLLATE pg_catalog."default",
    #     "addr:housenumber" character varying COLLATE pg_catalog."default",
    #     "addr:postcode" character varying COLLATE pg_catalog."default",
    #     "addr:street" character varying COLLATE pg_catalog."default",
    #     area character varying COLLATE pg_catalog."default",
    #     barrier character varying COLLATE pg_catalog."default",
    #     building character varying COLLATE pg_catalog."default",
    #     "building:levels" character varying COLLATE pg_catalog."default",
    #     fence_type character varying COLLATE pg_catalog."default",
    #     fixme character varying COLLATE pg_catalog."default",
    #     frequency character varying COLLATE pg_catalog."default",
    #     height character varying COLLATE pg_catalog."default",
    #     landuse character varying COLLATE pg_catalog."default",
    #     layer character varying COLLATE pg_catalog."default",
    #     location character varying COLLATE pg_catalog."default",
    #     name character varying COLLATE pg_catalog."default",
    #     "name:abbreviation" character varying COLLATE pg_catalog."default",
    #     "name:en" character varying COLLATE pg_catalog."default",
    #     operator character varying COLLATE pg_catalog."default",
    #     "operator:2" character varying COLLATE pg_catalog."default",
    #     "operator:wikidata" character varying COLLATE pg_catalog."default",
    #     "operator:wikipedia" character varying COLLATE pg_catalog."default",
    #     power character varying COLLATE pg_catalog."default",
    #     "railway:ref" character varying COLLATE pg_catalog."default",
    #     ref character varying COLLATE pg_catalog."default",
    #     "roof:colour" character varying COLLATE pg_catalog."default",
    #     "roof:shape" character varying COLLATE pg_catalog."default",
    #     short_name character varying COLLATE pg_catalog."default",
    #     source character varying COLLATE pg_catalog."default",
    #     substation character varying COLLATE pg_catalog."default",
    #     voltage character varying COLLATE pg_catalog."default",
    #     geom geometry(MultiPolygon,3035),
    #     CONSTRAINT transformers_pkey PRIMARY KEY (ogc_fid)
    # )""",
    "ways": """CREATE TABLE IF NOT EXISTS public.ways
(
    clazz integer,
    source integer,
    target integer,
    cost double precision,
    reverse_cost double precision,
    geom geometry(Geometry,3035),
    id integer NOT NULL
)""",
    "ways_result": """CREATE TABLE IF NOT EXISTS public.ways_result
(
    version_id character varying(10) NOT NULL,
    clazz integer,
    source integer,
    target integer,
    cost double precision,
    reverse_cost double precision,
    geom geometry(Geometry,3035),
    id integer NOT NULL,
    plz character varying(5)
)""",
    "ways_tem": """CREATE TABLE IF NOT EXISTS public.ways_tem
(
    clazz integer,
    source integer,
    target integer,
    cost double precision,
    reverse_cost double precision,
    geom geometry(Geometry,3035),
    id integer,
    plz character varying(5)
)""",
    "grids": """CREATE TABLE IF NOT EXISTS public.grids
    (
    version_id character varying(10) NOT NULL,
    plz character varying(5) NOT NULL,
    kcid integer NOT NULL,
    bcid integer NOT NULL,
    grid json NOT NULL,
    CONSTRAINT grids_pkey PRIMARY KEY (version_id, plz, kcid, bcid)
      )""",
    "version": """CREATE TABLE IF NOT EXISTS public.version
(
    version_id character varying(10) NOT NULL,
    version_comment character varying, 
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    consumer_categories character varying,
    cable_cost_dict character varying,
    connection_available_cables character varying,   
    other_parameters character varying, 
    CONSTRAINT version_pkey PRIMARY KEY (version_id)
)""",
    "grid_parameters": """CREATE TABLE IF NOT EXISTS public.grid_parameters
    (
    version_id character varying(10) NOT NULL,
    plz character varying(5) NOT NULL,
    trafo_num json,
    cable_length json,
    load_count_per_trafo json,
    bus_count_per_trafo json,
    sim_peak_load_per_trafo json,
    max_distance_per_trafo json,
    avg_distance_per_trafo json,
    CONSTRAINT parameters_pkey PRIMARY KEY (version_id, plz)
     )""",
}
