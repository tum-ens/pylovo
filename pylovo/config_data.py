import os
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv(), override=True)

# Database connection configuration
DBNAME = os.getenv("DBNAME", "pylovo_db_local")
USER = os.getenv("USER", "postgres")
HOST = os.getenv("HOST", "localhost")
PORT = os.getenv("PORT", "5432")
PASSWORD = os.getenv("PASSWORD", "postgres")

# Directory where the result csv and json files be saved
RESULT_DIR = os.path.join(os.getcwd(), "results")

# Toggles whether the grid json files will be saved in a folder or just in the db
SAVE_GRID_FOLDER = False

# Logging configuration for PgReaderWriter & GridGenerator
LOG_LEVEL = "INFO"

CSV_FILE_LIST = [
    {"path": os.path.join("raw_data", "equipment_data.csv"), "table_name": "betriebsmittel"},
    {"path": os.path.join("raw_data", "postcode.csv"), "table_name": "postcode"},
]

CLUSTERING_PARAMETERS = ["version_id",
                         "plz",
                         "bcid",
                         "kcid",
                         "no_connection_buses",
                         "no_branches",
                         "no_house_connections",
                         "no_house_connections_per_branch",
                         "no_households",
                         "no_household_equ",
                         "no_households_per_branch",
                         "max_no_of_households_of_a_branch",
                         "house_distance_km",
                         "transformer_mva",
                         "osm_trafo",
                         "max_trafo_dis",
                         "avg_trafo_dis",
                         "cable_length_km",
                         "cable_len_per_house",
                         "max_power_mw",
                         "simultaneous_peak_load_mw",
                         "resistance",
                         "reactance",
                         "ratio",
                         "vsw_per_branch",
                         "max_vsw_of_a_branch"]

MUNICIPAL_REGISTER = ['plz', 'pop', 'area', 'lat', 'lon', 'ags', 'name_city', 'fed_state', 'regio7', 'regio5',
                      'pop_den']

# Database schema - table structure
CREATE_QUERIES = {
    "res": """CREATE TABLE IF NOT EXISTS public.res
(
    ogc_fid integer,
    osm_id varchar(80),
    area numeric(23, 15),
    use varchar(80),
    comment varchar(80),
    free_walls numeric(18),
    building_t varchar(80),
    occupants numeric(23, 15),
    floors numeric(18),
    constructi varchar(80),
    refurb_wal numeric(23, 15),
    refurb_roo numeric(23, 15),
    refurb_bas numeric(23, 15),
    refurb_win numeric(23, 15),
    geom geometry(MultiPolygon,3035)
)""",
    "oth": """CREATE TABLE IF NOT EXISTS public.oth
(
    ogc_fid integer,
    osm_id varchar(80),
    area numeric(23, 15),
    use varchar(80),
    comment varchar(80),
    free_walls numeric(18),
    geom geometry(MultiPolygon,3035)
)""",
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
    kcid integer NOT NULL,
    bcid bigint NOT NULL,
    plz integer,
    s_max bigint,
    model_status integer,
    ont_vertice_id bigint,
    CONSTRAINT building_clusters_pkey PRIMARY KEY (version_id, kcid, bcid, plz)
)""",
    "lines_result": """
CREATE TABLE IF NOT EXISTS public.lines_result
(   version_id character varying(10) NOT NULL, 
    geom geometry(Geometry,3035),
    plz integer,
    bcid integer,
    kcid integer,
    line_name varchar(15),
    std_type varchar(15),
    from_bus integer,
    to_bus integer,
    length_km numeric
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
    peak_load_in_kw numeric,
    plz integer,
    vertice_id integer,
    bcid integer,
    kcid integer,
    floors integer,
    connection_point integer
    ,
    CONSTRAINT buildings_result_pkey PRIMARY KEY (version_id, osm_id)
)""",
    "sample_set": """CREATE TABLE IF NOT EXISTS public.sample_set
    (classification_id numeric,
    plz integer,
    pop numeric,
    area numeric,
    lat numeric,
    lon numeric,
    ags integer,
    name_city character varying(86),
    fed_state integer,
    regio7 integer,
    regio5 integer,
    pop_den numeric,
    bin_no int,
    bins numeric,
    perc_bin numeric,
    count numeric,
    perc numeric,
    CONSTRAINT sample_set_pkey PRIMARY KEY (classification_id, plz)
    )""",
    "classification_version": """CREATE TABLE IF NOT EXISTS public.classification_version
    (classification_id character varying(10) NOT NULL,
    version_comment character varying, 
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    classification_region character varying,
    CONSTRAINT classification_pkey PRIMARY KEY (classification_id)
    )""",
    "municipal_register": """CREATE TABLE IF NOT EXISTS public.municipal_register     
    (plz integer,
    pop numeric,
    area numeric,
    lat numeric,
    lon numeric,
    ags integer,
    name_city character varying(86),
    fed_state integer,
    regio7 integer,
    regio5 integer,
    pop_den numeric,
    CONSTRAINT municipal_register_pkey PRIMARY KEY (plz, ags)
    )""",
    "clustering_parameters": """CREATE TABLE IF NOT EXISTS public.clustering_parameters
    (
              version_id character varying(10) NOT NULL,
              plz integer NOT NULL,
              kcid integer NOT NULL ,
              bcid integer NOT NULL,

              no_connection_buses integer,
              no_branches integer,

              no_house_connections integer,
              no_house_connections_per_branch numeric,
              no_households integer,
              no_household_equ numeric,
              no_households_per_branch numeric,
              max_no_of_households_of_a_branch numeric,
              house_distance_km numeric,

              transformer_mva numeric,
              osm_trafo bool,

              max_trafo_dis numeric,
              avg_trafo_dis numeric,

              cable_length_km numeric,
              cable_len_per_house numeric,

              max_power_mw numeric,
              simultaneous_peak_load_mw numeric,

              resistance numeric,
              reactance numeric,
              ratio numeric,
              vsw_per_branch numeric,
              max_vsw_of_a_branch numeric,
              
              filtered boolean,
              CONSTRAINT clustering_parameters_pkey PRIMARY KEY (version_id, plz, bcid, kcid)
    )
    """,
    "buildings_tem": """CREATE TABLE IF NOT EXISTS public.buildings_tem
(
    osm_id character varying(80) COLLATE pg_catalog."default",
    area numeric,
    type character varying(80) COLLATE pg_catalog."default",
    geom geometry(Geometry,3035),
    houses_per_building integer,
    center geometry(Geometry,3035),
    peak_load_in_kw numeric,
    plz int,
    vertice_id bigint,
    bcid bigint,
    kcid integer,
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
    plz int,
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
                        plz integer,
                        kcid integer,
                        bcid integer,
                        geom geometry(Geometry,3035),
                        ogc_fid character varying(50),
                        "comment" character varying
                        )
                        """,
    "transformer_classified": """CREATE TABLE IF NOT EXISTS public.transformer_classified 
                (   version_id character varying(10) NOT NULL, 
                    plz integer,
                    kcid integer,
                    bcid integer,
                    geom geometry(Geometry,3035),
                    kmedoid_clusters integer,
                    kmedoid_representative_grid bool,
                    kmeans_clusters integer,
                    kmeans_representative_grid bool,
                    gmm_clusters integer,
                    gmm_representative_grid bool,
                    classification_id varchar(10)
                    )
                    """,
    "ags_log": """CREATE TABLE IF NOT EXISTS public.ags_log
                (   ags bigint NOT NULL, 
                    CONSTRAINT "ags_pkey" PRIMARY KEY (ags))
                    """,
    "transformers": """CREATE TABLE IF NOT EXISTS public.transformers
    (
        ogc_fid SERIAL,
        osm_id character varying COLLATE pg_catalog."default",
        area double precision,
        power character varying COLLATE pg_catalog."default",
        geom_type character varying COLLATE pg_catalog."default",
        within_shopping boolean,
        geom geometry(MultiPoint, 3035),
        CONSTRAINT transformers_pkey PRIMARY KEY (ogc_fid)
    )""",
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
    plz integer
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
    plz integer
)""",
    "grids": """CREATE TABLE IF NOT EXISTS public.grids
    (
    version_id character varying(10) NOT NULL,
    plz integer NOT NULL,
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
    plz integer NOT NULL,
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
