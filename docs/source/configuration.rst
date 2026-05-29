Configuration Overview
**********************

Pylovo uses a modular configuration system with separate YAML files for different aspects of the tool. This approach provides better organization and makes it easier to manage different types of settings.

Configuration Files
===================

The configuration system consists of the following files located in the ``config/`` directory:

.. list-table::
   :widths: 30 70
   :header-rows: 1

   * - File
     - Purpose
   * - ``config_database.yaml``
     - Database connection settings and InfDB configuration
   * - ``config_generation.yaml``
     - Grid generation parameters, regional settings, and transformer/cable definitions
   * - ``config_analysis.yaml``
     - Analysis and plotting configuration, municipal register settings
   * - ``config_classification.yaml``
     - Classification parameters and regional dictionaries
   * - ``config_clustering.yaml``
     - Clustering algorithms and threshold settings

Environment Variables
=====================

Database credentials are managed through environment variables in a ``.env`` file in the project root. This approach keeps sensitive information separate from the configuration files.

Required Environment Variables
------------------------------

For the main Pylovo database:

.. code-block:: bash

    # PYLOVO Database
    DBNAME="pylovo_db"
    DBUSER="postgres"
    HOST="localhost"
    PORT="5432"
    PASSWORD="yourpassword"

Optional Environment Variables
-----------------------------

For InfDB integration (when ``USE_INFDB: True`` in ``config_database.yaml``):

.. code-block:: bash

    # InfDB Database (Input Data)
    INFDB_DBNAME="citydb"
    INFDB_USER="citydb_user"
    INFDB_HOST="00.000.00.000"
    INFDB_PORT="5432"
    INFDB_PASSWORD="citydb_password"
    INFDB_SOURCE_SCHEMA="pylovo_input"

Configuration File Details
=========================

Database Configuration (config_database.yaml)
---------------------------------------------

Controls database connections and InfDB integration:

.. code-block:: yaml

    # Set to True if you want to use the InfDB database for building data
    USE_INFDB: True

Key settings:
- ``USE_INFDB``: Enable/disable InfDB integration
- Database connection examples and setup instructions

Grid Generation Configuration (config_generation.yaml)
-----------------------------------------------------

Main configuration file for grid generation with the following sections:

**Regional Configuration:**
- ``PLZ``: Postal code(s) for grid generation
- ``AGS``: Official municipality code(s)
- ``TESTING``: Enable testing mode with reduced data

**Execution Configuration:**
- ``PARALLEL``: Enable parallel processing
- ``N_JOBS_PERCENT``: Percentage of CPU cores to use
- ``ANALYZE_GRIDS``: Enable grid analysis after generation
- ``SAVE_GRID_FOLDER``: Save grid JSON files to folder
- ``LOG_LEVEL``: Logging level (DEBUG, INFO, ERROR)
- ``RESULT_DIR``: Directory for storing results

**Grid Generation Parameters:**
- ``VERSION_ID``: Unique identifier for grid version
- ``VERSION_COMMENT``: Description of grid parameters
- ``PEAK_LOAD_HOUSEHOLD``: Peak load per household (kW)
- ``SIM_FACTOR``: Simultaneous load factors by building type

**Consumer Categories:**
- Defines load calculation parameters for different building types
- Includes peak loads, consumption rates, and simultaneous factors

**Equipment Data:**
- ``TRANSFORMERS`` defines available transformer types
- ``FEEDER_CABLES`` defines cables allowed for feeder and backbone routing
- ``CONSUMER_CONNECTION_CABLES`` defines cables available for house connections

**Cable Dimensioning:**
- Voltage drop limits and thresholds
- Nominal voltage settings

**Settlement Type Thresholds:**
- Parameters for distinguishing rural vs urban areas

Analysis Configuration (config_analysis.yaml)
---------------------------------------------

Settings for grid analysis and visualization:

**Plotting Configuration:**
- ``PLOT_COLOR_DICT``: Color mapping for different transformer sizes

**Municipal Register:**
- ``MUNICIPAL_REGISTER``: Columns to include in regional data analysis

Classification Configuration (config_classification.yaml)
--------------------------------------------------------

Parameters for grid classification:

- ``CLASSIFICATION_VERSION``: Unique identifier for classification
- ``CLASSIFICATION_VERSION_COMMENT``: Description of classification approach
- ``CLASSIFICATION_REGION``: Target region for classification
- ``NO_OF_CLUSTERS_ALLOWED``: Valid number of clusters
- ``N_SAMPLES``: Number of samples for analysis
- ``REGION_DICT``: Mapping of region IDs to names
- ``REGIOSTAR7_DICT``: Regiostar 7 classification mapping
- ``REGIO7_REGIO5_GEM_DICT``: Regiostar 7 to 5 mapping

Clustering Configuration (config_clustering.yaml)
------------------------------------------------

Settings for clustering algorithms:

**Clustering Parameters:**
- ``LIST_OF_CLUSTERING_PARAMETERS``: Parameters used for clustering
- ``CLUSTERING_PARAMETERS``: All available clustering parameters

**Algorithm Settings:**
- ``N_CLUSTERS_KMEDOID``: Number of clusters for K-Medoids
- ``N_CLUSTERS_KMEANS``: Number of clusters for K-Means
- ``N_CLUSTERS_GMM``: Number of clusters for Gaussian Mixture Model

**Thresholds:**
- Various threshold values for filtering and analysis

Configuration Loading
=====================

The configuration system is loaded through ``src/config_loader.py``, which:

1. Loads all YAML configuration files
2. Reads environment variables from ``.env`` file
3. Validates required settings and cable-list consistency
4. Provides centralized access to all configuration values

The loader automatically:
- Resolves placeholder references (e.g., ``PEAK_LOAD_HOUSEHOLD``)
- Converts data to appropriate formats (DataFrames, lists, etc.)
- Provides clear error messages for missing required settings

Best Practices
==============

1. **Version Control**: Keep configuration files in version control, but exclude ``.env`` files
2. **Environment Separation**: Use different ``.env`` files for different environments
3. **Parameter Validation**: Always validate configuration values before running processes
4. **Documentation**: Update configuration documentation when adding new parameters
5. **Backup**: Keep backup copies of working configurations

Troubleshooting
==============

Common configuration issues:

1. **Missing Environment Variables**: Check that all required variables are set in ``.env``
2. **File Not Found**: Ensure all configuration files exist in the ``config/`` directory
3. **Invalid YAML**: Validate YAML syntax in configuration files
4. **Database Connection**: Verify database credentials and connectivity
5. **Path Issues**: Ensure relative paths in configuration are correct

For more detailed information about specific configuration options, refer to the individual configuration files and their inline documentation.
