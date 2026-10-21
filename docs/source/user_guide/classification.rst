Classifying grids
=================

The classification groups the generated grids of a region into a few **representative grid types**.
It draws a sample of postcode areas (PLZ) that mirrors the settlement structure of the region,
generates and analyses their grids, condenses every grid to a handful of key figures and clusters
the grids with k-means and a Gaussian mixture model. For every cluster it names one real grid as
representative. The result is stored per transformer position in ``pylovo.transformer_classified``
and can be shown in QGIS.

The classification is a separate workflow. Grid generation does not need it, and the browser UI does
not cover it (:doc:`browser_ui`).

At a glance
-----------

.. list-table::
   :header-rows: 1
   :widths: 6 34 60
   :class: fixed-table

   * - Step
     - What happens
     - Stored in
   * - 1
     - Sample set: PLZ drawn per RegioStaR 7 class.
     - ``classification_version``, ``sample_set``
   * - 2
     - Buildings imported from shapefiles (only with ``USE_INFDB=False``).
     - ``res``, ``oth``
   * - 3
     - Grids generated and the PLZ analysed for the sample (PLZ that already exist for ``VERSION_ID`` are kept).
     - ``grid_result``, ``plz_parameters``, ...
   * - 4
     - Key figures per grid (:doc:`analysing_grids`).
     - ``clustering_parameters``
   * - 5
     - Implausible grids marked as filtered.
     - ``clustering_parameters.filtered``
   * - 6
     - Clustering parameters chosen with a factor analysis, number of clusters chosen with the Calinski-Harabasz index.
     - ``config_clustering.yaml``
   * - 7
     - Grids clustered, representative grids chosen, result written.
     - ``transformer_classified``, ``transformer_classified_with_grid``

Steps 1 to 5 run in ``prepare_data_for_clustering``, step 7 in ``apply_clustering_for_visualisation``.

Before you start
----------------

* The schema is set up and ``pylovo.municipal_register`` is filled. ``pylovo-setup`` builds the register from
  ``data/municipal_register`` (population and area per PLZ, PLZ to municipality, RegioStaR classes), see
  :doc:`database_setup`.
* The input data of the region cover the PLZ of the sample (:doc:`input_data`). A PLZ whose generation fails,
  for example because the data do not cover it, is logged, skipped and removed from the sample set.
* The plotting extra is installed: ``uv sync --extra plots``. The clustering helpers import matplotlib, seaborn
  and plotly, so without the extra ``pylovo-classify`` stops at the start and asks for it. The notebooks need
  ``--extra notebooks``. ``factor-analyzer`` and scikit-learn are core dependencies.
* The grids come from the ``VERSION_ID`` of ``config_generation_lv.yaml`` (:ref:`versions`). Its configuration
  must still match the stored snapshot, otherwise step 3 stops with ``Generation parameters differ from the
  stored snapshot``.
* A full sample of about 100 PLZ takes hours up to a day because every PLZ is generated. With the answer *yes* to
  "restrict sampling to PLZ present in postcode_result" only PLZ that already have grids are drawn, and step 3
  has nothing left to generate.

Configuration
-------------

Both files are looked up like the other YAML files (:ref:`config-file-lookup`), normally in ``./config``. Their
values are read at import, so a changed value needs a new Python process.

``config_classification.yaml``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 34 66
   :class: fixed-table

   * - Key
     - Meaning
   * - ``CLASSIFICATION_VERSION``
     - Integer id of the classification. Every run needs a new one: an existing id stops the run.
   * - ``CLASSIFICATION_VERSION_COMMENT``
     - Free text stored in ``classification_version``. Describe the idea and the parameters.
   * - ``CLASSIFICATION_REGION``
     - ``Germany`` or one value of ``REGION_DICT`` (``Bayern``, ``Hessen``, ...). It limits the PLZ that can be drawn.
   * - ``N_SAMPLES``
     - Approximate number of PLZ in the sample. The shipped ``7`` is a test value that leaves small RegioStaR
       classes empty. Use a larger number for a federal state, for example 100.
   * - ``NO_OF_CLUSTERS_ALLOWED``
     - Cluster numbers that the Calinski-Harabasz index compares and that manual input accepts
       (shipped ``[3, 4, 5, 6, 7]``).
   * - ``REGION_DICT``, ``REGIOSTAR7_DICT``, ``REGIO7_REGIO5_GEM_DICT``
     - Lookup tables (federal states, the seven RegioStaR classes, mapping to the five RegioStaR Gem classes).
       Leave them unchanged.

``config_clustering.yaml``
~~~~~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 34 66
   :class: fixed-table

   * - Key
     - Meaning
   * - ``LIST_OF_CLUSTERING_PARAMETERS``
     - Columns of ``clustering_parameters`` that the clustering uses. ``pylovo-classify`` writes it.
   * - ``N_CLUSTERS_KMEANS``, ``N_CLUSTERS_GMM``
     - Number of clusters of the two algorithms. ``pylovo-classify`` writes them.
   * - ``N_CLUSTERS_KMEDOID``
     - Not used any more (k-medoids was removed). The key must still exist in the file.
   * - ``THRESHOLD_MAX_TRAFO_DIS``
     - Always applied: grids with a path from the transformer to a house connection longer than this value (km)
       are filtered. Shipped ``2.5``.
   * - ``THRESHOLD_HOUSEHOLDS_PER_BUILDING``
     - Always applied: grids with a building of more households than this are filtered (buildings wrongly
       classified as residential). Shipped ``150``.
   * - ``THRESHOLD_AVG_TRAFO_DIS``, ``THRESHOLD_NO_HOUSE_CONNECTIONS``, ``THRESHOLD_VSW_PER_BRANCH``,
       ``THRESHOLD_NO_HOUSEHOLDS``
     - Lower bounds, applied only with additional filtering. A grid below any of them is filtered. They remove the
       small filling grids at the edge of a PLZ. The shipped values (0.115, 14.332, 0.258, 35.316) are the means
       of k-means cluster 0 of an earlier run.
   * - ``CLUSTERING_PARAMETERS``
     - All columns that are read from the database. Leave it unchanged.

Run the classification
----------------------

Set a new ``CLASSIFICATION_VERSION`` and a comment in ``config/config_classification.yaml``, then run from the
repository root:

.. code-block:: bash

   uv run --extra plots pylovo-classify

The command takes no options. It asks three questions and then runs without further input. Answer ``yes`` or
``no`` (``y`` and ``n`` work as well):

.. list-table::
   :header-rows: 1
   :widths: 46 54
   :class: fixed-table

   * - Question
     - Effect
   * - Apply additional filtering on top of the default filters?
     - *yes* adds the four lower thresholds to the filter of step 5.
   * - Restrict sampling to PLZ present in postcode_result for the configured ``VERSION_ID``?
     - *yes* draws only PLZ that already have grids, so step 3 generates nothing.
   * - Assign clustering parameters and number of clusters yourself?
     - *no*: both are computed (steps 6a and 6b). *yes*: you enter the parameter names, comma-separated, and
       three cluster numbers from ``NO_OF_CLUSTERS_ALLOWED``. The prompt still says "KMedoid, KMeans, and GMM",
       so the first number is ignored. The second is used for k-means, the third for the GMM.

The chosen parameters and cluster numbers are written to ``config/config_clustering.yaml`` and handed to the
clustering step directly. The file is rewritten as a whole, so comments in it are lost.

The run stops immediately with ``Classification version: N already exists. Create a new one.`` if the id is taken.

How the steps work
------------------

Step 1: sample set
~~~~~~~~~~~~~~~~~~

The sample follows the settlement structure with the RegioStaR 7 classification of the Federal Ministry for
Digital and Transport (BMDV):

.. list-table::
   :header-rows: 1
   :widths: 12 88
   :class: fixed-table

   * - Class
     - Type
   * - 71
     - Stadtregion, Metropole
   * - 72
     - Stadtregion, Regiopole und Großstadt
   * - 73
     - Stadtregion, Mittelstadt, städtischer Raum
   * - 74
     - Stadtregion, kleinstädtischer, dörflicher Raum
   * - 75
     - Ländliche Region, zentrale Stadt
   * - 76
     - Ländliche Region, städtischer Raum
   * - 77
     - Ländliche Region, kleinstädtischer, dörflicher Raum

The register has one row per PLZ and municipality. A PLZ that spans several municipalities keeps its first row.
The pool is limited to the federal state of ``CLASSIFICATION_REGION`` (not for ``Germany``) and, if you answered
*yes*, to PLZ with a ``postcode_result`` of ``VERSION_ID``.

* **Quota per class.** ``round(N_SAMPLES x population share of the class)``. The rounding makes the total differ
  slightly from ``N_SAMPLES``, and a class with less than about 7 % of the population gets no PLZ at
  ``N_SAMPLES = 7``. The quota must not exceed the number of PLZ of the class.
* **Draw within a class.** The population densities of the class are split into 5 bins (fewer than 100 PLZ) or 10
  bins. Every PLZ is drawn without replacement with the probability *share of its bin / PLZ in the bin*, so the
  sample reproduces the density distribution of the class.

The result is stored in ``classification_version`` (id, comment, region, time) and ``sample_set`` (PLZ, AGS and
the bin data). The draw is random and not seeded, so ``sample_set`` is the only record of the sample.

Steps 2 and 3: buildings and grids
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

With ``USE_INFDB=False`` the building shapefiles of the sampled municipalities are imported first. With InfDB
the generation reads the buildings itself.

Then ``GridGenerator.generate_grid_for_multiple_plz`` generates the grids of the sample like ``pylovo-generate``
does (:doc:`generating_grids`), in parallel worker processes (``N_JOBS_PERCENT`` of the CPU cores, independent of
``PARALLEL``) and with the PLZ analysis (``analyze_grids=True``). A PLZ that already has grids for ``VERSION_ID``
is kept as it is and not analysed again, so grids that were generated without ``ANALYZE_GRIDS`` need
``pylovo-analyze --plz <plz>`` before.

Steps 4 and 5: key figures and filter
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Step 4 computes one row of ``clustering_parameters`` for every grid of every sampled PLZ
(``ParameterCalculator.analyze_grid_parameters_for_plz``, columns in :doc:`analysing_grids`). Grids that already
have a row are skipped. A PLZ without PLZ analysis and a grid whose calculation fails are logged and skipped.

Step 5 sets ``clustering_parameters.filtered``:

* ``true`` for grids with ``max_trafo_dis > THRESHOLD_MAX_TRAFO_DIS``,
* ``true`` for grids with a building of more than ``THRESHOLD_HOUSEHOLDS_PER_BUILDING`` households,
* with additional filtering also ``true`` for grids below one of the four lower thresholds,
* ``false`` for all other grids that have no flag yet.

Only grids with ``filtered = false`` of the sampled PLZ and of ``VERSION_ID`` enter steps 6 and 7.

Step 6a: clustering parameters
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Many key figures are strongly correlated, so the clustering uses a few independent ones. The automatic choice

#. drops identifiers and parameters that are not meant for clustering (``version_id``, ``plz``, ``bcid``,
   ``kcid``, ``ratio``, ``osm_trafo``, ``house_distance_km``, ``no_connection_buses``, ``resistance``,
   ``reactance``, ``simultaneous_peak_load_mw``, ``no_household_equ``, ``max_power_mw``), which leaves 13 candidates,
#. removes columns that are perfectly collinear with an earlier column,
#. takes the number of factors as the number of eigenvalues of the correlation matrix above 1,
#. runs a varimax-rotated factor analysis on the standardised data with that many factors and
#. picks for every factor the parameter with the largest absolute loading.

The names are printed and written to ``LIST_OF_CLUSTERING_PARAMETERS``. You can overrule them: the notebook
``1_0_factor_analysis`` shows the loadings, and other considerations may decide. The shipped list is
``no_house_connections``, ``vsw_per_branch`` and ``avg_trafo_dis``. It needs enough grids: a handful of grids
from one PLZ makes the factor analysis fail.

Step 6b: number of clusters
~~~~~~~~~~~~~~~~~~~~~~~~~~~

The Calinski-Harabasz index (higher is better) is computed for the numbers in ``NO_OF_CLUSTERS_ALLOWED`` for
k-means and for four GMM covariance types (full, diagonal, tied, spherical). The best number of ``kmeans`` becomes
``N_CLUSTERS_KMEANS`` and the best of ``gmm_tied`` becomes ``N_CLUSTERS_GMM``. A line plot is drawn but not saved.
The notebooks ``1_CH_index`` and ``2_DB_index`` compare the Calinski-Harabasz and the Davies-Bouldin index.

Step 7: clustering and result
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The parameters are standardised, then the grids are clustered with k-means (``random_state=0``) and with a Gaussian
mixture model with tied covariance (``random_state=1``). The **representative grid** of a cluster is the real grid
closest to the cluster centre. The clusters are numbered by the number of households of their representative grid:
cluster 0 has the smallest representative. K-medoids was removed to avoid the ``scikit-learn-extra``
dependency.

The result is appended to ``pylovo.transformer_classified`` with the position of the transformer of every grid:

.. list-table::
   :header-rows: 1
   :widths: 34 66
   :class: fixed-table

   * - Column
     - Content
   * - ``grid_result_id``, ``classification_id``
     - The grid and the classification (together the primary key).
   * - ``geom``
     - Transformer position (point, ``TARGET_EPSG``).
   * - ``kmeans_clusters``, ``kmeans_representative_grid``
     - K-means cluster of the grid and whether the grid is its representative.
   * - ``gmm_clusters``, ``gmm_representative_grid``
     - The same for the Gaussian mixture model (tied).
   * - ``kmedoid_clusters``, ``kmedoid_representative_grid``
     - Always empty (``NULL`` and ``false``), kept for old databases.

The materialized view ``pylovo.transformer_classified_with_grid`` adds ``version_id``, ``plz``, ``kcid``,
``bcid``, the transformer rating and its equipment to every row and is refreshed after the write. Add it as a
layer in QGIS, filter on ``classification_id`` and colour by ``kmeans_clusters`` or ``gmm_clusters``.

Look at the result
------------------

The representative grids of one algorithm:

.. code-block:: sql

   SELECT tc.kmeans_clusters, gr.plz, gr.kcid, gr.bcid, gr.transformer_rated_power
   FROM pylovo.transformer_classified tc
   JOIN pylovo.grid_result gr USING (grid_result_id)
   WHERE tc.classification_id = 1 AND tc.kmeans_representative_grid
   ORDER BY tc.kmeans_clusters;

Load one of them as a pandapower net with
``DatabaseClient().read_net_db(plz, kcid, bcid)`` (:doc:`analysing_grids`) or plot it with the helpers in
``pylovo.plotting.generation.networks`` (:doc:`exporting_visualising`).

The notebooks in ``notebook_tutorials/classification`` walk through the steps. They read the sample set, the grids and
the key figures of the ``CLASSIFICATION_VERSION`` and ``VERSION_ID`` of the configuration, need the ``notebooks``
extra, and their stored outputs come from an earlier run on an older pylovo version.

.. list-table::
   :header-rows: 1
   :widths: 42 58
   :class: fixed-table

   * - Notebook
     - Content
   * - ``sampling/analyse_sampling_results``
     - The sampled PLZ on a map and per RegioStaR 7 class.
   * - ``plz_parameter_analysis/analyse_clustering_parameters``
     - Pair plot of the key figures, grids sorted by a figure, one grid on a map.
   * - ``plz_parameter_analysis/vsw_analysis``
     - Distribution of the largest consumer sum resistance (VSW) per branch and the grids with the highest values.
   * - ``correlation_and_factor_analysis/1_0_factor_analysis``
     - Adequacy tests, number of factors, loadings and the proposed parameters.
   * - ``correlation_and_factor_analysis/1_1_explained_variance_eigen_decomposition``
     - Explained variance per component.
   * - ``correlation_and_factor_analysis/1_2_correlation_matrix``
     - Correlation matrix and cluster map of the key figures.
   * - ``indices/1_CH_index``, ``indices/2_DB_index``
     - Calinski-Harabasz and Davies-Bouldin index for k-means and the GMM variants.
   * - ``indices/3_Compare_clusterings_rand_index``
     - Adjusted Rand index between the k-means and the GMM clustering.
   * - ``clustering/2_0_kmeans_representative_grids``, ``clustering/3_0_gmm_representative_grids``
     - Radar plot and plots of the representative grids.
   * - ``clustering/2_1_kmeans_clusters``, ``clustering/3_1_gmm_clusters``
     - Share of the clusters, distribution over the RegioStaR classes, pair plots.

``notebook_tutorials/classification/config-example`` holds example values for both YAML files.

Use the steps from Python
-------------------------

The command is a thin layer over these functions. They read the configuration constants once at import:

.. code-block:: python

   from pylovo.classification.clustering.prepare_data_for_clustering import prepare_data_for_clustering
   from pylovo.classification.clustering.get_parameters_for_clustering import get_parameters_for_clustering
   from pylovo.classification.clustering.get_no_clusters_for_clustering import get_no_clusters_for_clustering
   from pylovo.classification.clustering.apply_clustering_for_visualisation import apply_clustering_for_visualisation

   prepare_data_for_clustering(additional_filtering=False, sample_from_postcode_result_only=True)  # steps 1-5
   parameters = get_parameters_for_clustering()                # step 6a: list of column names
   best = get_no_clusters_for_clustering(parameters)           # step 6b: algorithm, no_clusters, ch_index
   apply_clustering_for_visualisation(                         # step 7
       list_of_clustering_parameters=parameters, n_clusters_kmeans=5, n_clusters_gmm=4)

``kmeans_clustering`` and ``gmm_tied_clustering`` in ``pylovo.classification.clustering.clustering_algorithms`` cluster any data
frame with the key figures and return the clustered frame and the representative grids.

Repeat or delete a classification
---------------------------------

.. code-block:: bash

   uv run pylovo-delete classification --version 1

removes the classification with its sample set and its result (``ON DELETE CASCADE``) and refreshes the
materialized view ``transformer_classified_with_grid``. The id is the integer ``CLASSIFICATION_VERSION``. The grids
and their key figures stay. To cluster again with other parameters or numbers under the same id, delete the rows of
the result and run step 7 once more:

.. code-block:: sql

   DELETE FROM pylovo.transformer_classified WHERE classification_id = 1;

Things to know
--------------

* The sample is drawn without a seed. Keep ``sample_set`` (or the database) as the record of the classification.
* ``clustering_parameters.filtered`` belongs to the grid, not to the classification. The filter statements touch the grids of all
  versions, and a flag is never reset, so a grid that an earlier run filtered stays filtered.
* Step 7 writes once per classification id (see above for repeating it).
* A failing PLZ is removed from the sample set of the configured ``CLASSIFICATION_VERSION`` by every generation or
  analysis run, not only by the classification.
* The classification version is registered before the draw. If the draw fails (unknown region name, a class with
  fewer PLZ than its quota), delete the version before the next try.
* The representative grid is the grid closest to the cluster centre. With strongly overlapping Gaussian components that grid can
  belong to another cluster. Check that every cluster id from 0 to k-1 has exactly one representative.
* The lower filter thresholds come from an earlier run and are not re-derived for the current generator.
  ``pylovo.classification.utils.get_average_values_clustering_parameters`` recomputes cluster means.

Troubleshooting
---------------

.. list-table::
   :header-rows: 1
   :widths: 38 62
   :class: fixed-table

   * - Message or symptom
     - Cause and remedy
   * - ``pylovo-classify`` stops at the start and asks for the plotting extra
     - Install it: ``uv sync --extra plots``.
   * - ``Classification version: N already exists. Create a new one.``
     - Set a new id in ``config_classification.yaml`` or run ``pylovo-delete classification --version N``.
   * - ``Generation parameters differ from the stored snapshot``
     - The generation configuration changed since ``VERSION_ID`` was created (:ref:`versions`).
   * - ``Cannot take a larger sample than population when 'replace=False'``
     - A class quota exceeds the PLZ of that class. Lower ``N_SAMPLES`` or widen the pool. Delete the half-created
       version first.
   * - ``array must not contain infs or NaNs`` or ``Input X contains NaN`` in step 6
     - Too few or degenerate grids, or a missing value in a key figure. Use a larger sample or enter parameters yourself.
   * - ``KeyError`` in step 7
     - A name in ``LIST_OF_CLUSTERING_PARAMETERS`` is not a column of ``clustering_parameters``.
   * - Duplicate key error when writing ``transformer_classified``
     - The classification already has a result. Delete it first (see above).
   * - Fewer grids than expected
     - PLZ without grids or without PLZ analysis are skipped, failing grids are only logged. Check ``log/log.txt``
       and the counts in ``grid_result``, ``plz_parameters`` and ``clustering_parameters``.
