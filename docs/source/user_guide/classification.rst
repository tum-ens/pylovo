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
     - Implausible grids filtered, applied each time the grids are read in steps 6 and 7.
     - ``config_clustering.yaml`` (``ADDITIONAL_FILTERING`` and the thresholds), nothing in the database
   * - 6
     - Clustering parameters chosen with a factor analysis, number of clusters chosen with the Calinski-Harabasz index.
     - ``config_clustering.yaml``
   * - 7
     - Grids clustered, representative grids chosen, result written.
     - ``transformer_classified``, ``transformer_classified_with_grid``

Steps 1 to 4 run in ``prepare_data_for_clustering``, step 7 in ``apply_clustering_for_visualisation``. A summary of
the grids per stage closes the run (:ref:`classification-summary`).

Before you start
----------------

* The schema is set up and ``pylovo.municipal_register`` is filled. ``pylovo-setup`` builds the register from
  ``data/municipal_register`` (population and area per PLZ, PLZ to municipality, RegioStaR classes), see
  :doc:`database_setup`.
* The input data of the region cover the PLZ of the sample (:doc:`input_data`). A PLZ whose generation fails,
  for example because the data do not cover it, is logged, skipped and removed from the sample set.
* The plotting extra is installed: ``uv sync --extra plots``. The clustering helpers import matplotlib, seaborn
  and plotly, so without the extra ``pylovo-classify`` stops at the start with a message that names the
  missing package and the extra. The notebooks need ``--extra notebooks``. ``factor-analyzer`` and
  scikit-learn are core dependencies.
* The grids come from the ``VERSION_ID`` of ``config_generation_lv.yaml`` (:ref:`versions`). Its configuration
  must still match the stored snapshot, otherwise step 3 stops with ``Generation parameters differ from the
  stored snapshot``.
* A full sample of about 100 PLZ takes hours up to a day because every PLZ is generated. With the answer *yes* to
  "restrict sampling to PLZ present in postcode_result" only PLZ that already have grids are drawn, and step 3
  has nothing left to generate.

Configuration
-------------

Both files are looked up like the other YAML files (:ref:`config-file-lookup`), normally in ``./config``. Their
values are read at import, so a changed value needs a new Python process. ``pylovo-classify`` edits only the values
it writes and keeps the comments and everything else in the file.

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
   * - ``REGION_DICT``, ``REGIO7_REGIO5_GEM_DICT``
     - Lookup tables (federal states, mapping of the seven RegioStaR classes to the five RegioStaR Gem classes).
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
   * - ``ADDITIONAL_FILTERING``
     - ``true`` or ``false`` (shipped ``false``, also when the key is missing). Whether the four lower thresholds below
       filter grids as well. The filters are applied each time the grids are read, so the notebooks and the Python
       functions need the value of the classification they look at. ``pylovo-classify`` writes the answer to its
       first question.
   * - ``THRESHOLD_MAX_TRAFO_DIS``
     - Always applied: grids with a path from the transformer to a house connection longer than this value (km)
       are filtered. Shipped ``2.5``.
   * - ``THRESHOLD_HOUSEHOLDS_PER_BUILDING``
     - Always applied: grids with a building of more households than this are filtered (buildings wrongly
       classified as residential). Shipped ``150``.
   * - ``THRESHOLD_AVG_TRAFO_DIS``, ``THRESHOLD_NO_HOUSE_CONNECTIONS``, ``THRESHOLD_VSW_PER_BRANCH``,
       ``THRESHOLD_NO_HOUSEHOLDS``
     - Lower bounds, applied only with additional filtering. A grid below any of them is filtered. They remove the
       small filling grids at the edge of a PLZ. The shipped values (0.115, 14.332, 0.258, 35.316) are the means of
       the four parameters over k-means cluster 0 of one classification over 100 PLZ that clustered with exactly these
       four parameters, on grids of pylovo 0.8.0. The generator has changed since (:ref:`classification-thresholds`).
   * - ``CLUSTERING_PARAMETERS``
     - All columns that are read from the database. Leave it unchanged.

Run the classification
----------------------

Set a new ``CLASSIFICATION_VERSION`` and a comment in ``config/config_classification.yaml``, then run from the
repository root:

.. code-block:: bash

   uv run --extra plots pylovo-classify

Without options the command first checks that the classification version is new, then asks three questions and runs
without further input. Answer ``yes`` or ``no`` (``y`` and ``n`` work as well, any other answer is asked again).
Every question has an option, so a run can be scripted:

.. list-table::
   :header-rows: 1
   :widths: 36 28 36
   :class: fixed-table

   * - Question
     - Option
     - Effect
   * - Apply additional filtering on top of the default filters?
     - ``--additional-filtering``, ``--no-additional-filtering``
     - *yes* adds the four lower thresholds to the filters of steps 6 and 7. The answer is written as
       ``ADDITIONAL_FILTERING``.
   * - Restrict sampling to PLZ present in postcode_result for the configured ``VERSION_ID``?
     - ``--restrict-to-generated``, ``--no-restrict-to-generated``
     - *yes* draws only PLZ that already have grids, so step 3 generates nothing.
   * - Assign clustering parameters and number of clusters yourself?
     - ``--auto``, or ``--manual`` with ``--parameters`` and ``--clusters``
     - *no* or ``--auto``: both are computed (steps 6a and 6b). *yes* or ``--manual``: the parameter names,
       comma-separated (columns of ``clustering_parameters``, without the identifiers ``version_id``, ``plz``,
       ``bcid`` and ``kcid``), and two cluster numbers from ``NO_OF_CLUSTERS_ALLOWED``, the first for k-means and the
       second for the GMM. The question asks for them, the options take ``--parameters NAME[,NAME...]`` and
       ``--clusters KMEANS,GMM``.

An option answers only its own question and the others are still asked. A run without any question:

.. code-block:: bash

   uv run --extra plots pylovo-classify --no-additional-filtering --restrict-to-generated \
       --manual --parameters no_branches,avg_trafo_dis --clusters 4,5

``--manual`` needs ``--parameters`` and ``--clusters`` (they need ``--manual``), and ``--auto`` and ``--manual`` exclude
each other. The names and numbers are checked at the start, so a typo stops the command before the hours of a run.
If standard input is closed while a question is open, the command stops with a message that points to the options.

The automatic path prints the selected parameters, the Calinski-Harabasz table (best number of clusters per
algorithm with its index) and the chosen numbers. The parameters, the numbers and ``ADDITIONAL_FILTERING`` are written
to ``config/config_clustering.yaml`` and handed to the clustering step directly. Only these values are edited in
the file, comments and the rest stay. A layout that this edit cannot handle (a changed key with a mapping as value) is
reported and the file is left alone.

The run stops before the first question with ``Classification version N already exists. Set a new
CLASSIFICATION_VERSION in ... (or delete the old one with 'pylovo-delete classification').`` if the id is taken.

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

The register has one row per PLZ and municipality. A PLZ that spans several municipalities keeps the row with the lowest AGS.
The pool is limited to the federal state of ``CLASSIFICATION_REGION`` (not for ``Germany``) and, if you answered
*yes*, to PLZ with a ``postcode_result`` of ``VERSION_ID``.

* **Quota per class.** ``round(N_SAMPLES x population share of the class)``. The rounding makes the total differ
  slightly from ``N_SAMPLES``, and a class with less than about 7 % of the population gets no PLZ at
  ``N_SAMPLES = 7``. The quota must not exceed the number of PLZ of the class.
* **Draw within a class.** The population densities of the class are split into 5 bins (fewer than 100 PLZ) or 10
  bins. Every PLZ is drawn without replacement with the probability *share of its bin / PLZ in the bin*, so the
  sample reproduces the density distribution of the class.

The result is stored in ``classification_version`` (id, comment, region, time) and ``sample_set`` (PLZ, AGS and
the bin data). The version is registered only after the draw succeeded: if a class has fewer PLZ than its quota,
the run stops with a message and stores nothing.

The draw is reproducible. The register is sorted by PLZ and AGS and the generator is seeded with
``CLASSIFICATION_VERSION`` (a non-negative integer), so the same version, ``N_SAMPLES``, region and register (and,
with the restriction, the same ``postcode_result``) draw the same PLZ with the same NumPy version, which does not
guarantee the stream of its generators across versions. Another version number gives another sample. ``sample_set``
remains the record of the sample.

Steps 2 and 3: buildings and grids
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

With ``USE_INFDB=False`` the building shapefiles of the sampled municipalities are imported first. With InfDB
the generation reads the buildings itself.

Then ``GridGenerator.generate_grid_for_multiple_plz`` generates the grids of the sample like ``pylovo-generate``
does (:doc:`generating_grids`), in parallel worker processes (``N_JOBS_PERCENT`` of the CPU cores, independent of
``PARALLEL``) and with the PLZ analysis (``analyze_grids=True``). A PLZ that already has grids for ``VERSION_ID``
is kept as it is and not analysed again, so grids that were generated without ``ANALYZE_GRIDS`` need
``pylovo-analyze --plz <plz>`` before. A PLZ for which no grids could be generated is logged and removed from
the sample set of this classification.

Steps 4 and 5: key figures and filter
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Step 4 computes one row of ``clustering_parameters`` for every grid of every sampled PLZ
(``ParameterCalculator.analyze_grid_parameters_for_plz``, columns in :doc:`analysing_grids`). Grids that already
have a row are skipped. A PLZ without PLZ analysis and a grid whose calculation fails are logged and skipped, and the
summary at the end counts them (:ref:`classification-summary`).

Step 5 stores nothing. The filters are applied each time the grids are read, by steps 6 and 7 and by the notebooks, to
the grids of the sampled PLZ and of ``VERSION_ID``. A grid is left out if

* ``max_trafo_dis > THRESHOLD_MAX_TRAFO_DIS``,
* it has a building with more than ``THRESHOLD_HOUSEHOLDS_PER_BUILDING`` households, or
* with ``ADDITIONAL_FILTERING`` it is below one of the four lower thresholds.

A key figure that is missing never filters a grid. The result depends only on the current key figures, the sample set
and the two YAML files, never on earlier runs, and classifications that share grids can use different filters. This
replaced a flag ``clustering_parameters.filtered`` that was set for the grids of all versions and classifications and
never reset. The column still exists in the table and is no longer used.

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
``no_house_connections``, ``vsw_per_branch`` and ``avg_trafo_dis``. It needs enough grids. If the factor analysis
fails (a very small sample, or a parameter that is the same in every grid), the command stops with a message that
names the number of grids and parameters: use a larger sample (``N_SAMPLES``) or enter the parameters yourself.

Step 6b: number of clusters
~~~~~~~~~~~~~~~~~~~~~~~~~~~

The Calinski-Harabasz index (higher is better) is computed for the numbers in ``NO_OF_CLUSTERS_ALLOWED`` for
k-means and for four GMM covariance types (full, diagonal, tied, spherical). The best number of ``kmeans`` becomes
``N_CLUSTERS_KMEANS`` and the best of ``gmm_tied`` becomes ``N_CLUSTERS_GMM``. A line plot is drawn but not saved, and the table of the best number per algorithm with its index is printed.
The notebooks ``1_CH_index`` and ``2_DB_index`` compare the Calinski-Harabasz and the Davies-Bouldin index.

Step 7: clustering and result
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The parameters are standardised, then the grids are clustered with k-means (``random_state=0``) and with a Gaussian
mixture model with tied covariance (``random_state=1``). The **representative grid** of a cluster is the real grid
closest to the cluster centre. The clusters are numbered by the number of households of their representative grid:
cluster 0 has the smallest representative. K-medoids was removed to avoid the ``scikit-learn-extra``
dependency.

The result is written to ``pylovo.transformer_classified`` with the position of the transformer of every grid. The rows
of the classification are deleted and inserted in one transaction, so the step can be repeated with other parameters or
numbers, and a failing write keeps the previous result:

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

.. _classification-summary:

Summary of the run
~~~~~~~~~~~~~~~~~~

At the end of the run ``report_grid_counts`` prints from the database how many grids every stage kept:

.. code-block:: text

   Grids of classification 9001 (VERSION_ID v1)
     PLZ drawn                 12
     PLZ in the sample set     11
     Grids of these PLZ       231
     Grids with key figures    93
     Grids filtered            10
       by max_trafo_dis > 2.5  10
     Grids clustered           83

and logs a ``WARNING`` for every loss that the filters do not explain:

* sampled PLZ that have no grids and were removed from the sample set (step 3),
* sampled PLZ without any grid for ``VERSION_ID``,
* grids without key figures, split into grids of PLZ without PLZ analysis (run ``pylovo-analyze --plz <plz>``) and
  grids whose calculation failed (details in ``log/log.txt``),
* a mismatch between the grids that pass the filters and the grids in ``transformer_classified``.

Filtered grids are listed per rule (a grid can match several) and are no warning, they are the intended filter. The
run still ends with "Classification process completed successfully!" when grids were lost, so read the summary.

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

The command is a thin layer over these functions. They read the configuration constants once at import.
``additional_filtering=None`` (the default) means ``ADDITIONAL_FILTERING`` of ``config_clustering.yaml``:

.. code-block:: python

   from pylovo.classification.clustering.prepare_data_for_clustering import prepare_data_for_clustering
   from pylovo.classification.clustering.get_parameters_for_clustering import get_parameters_for_clustering
   from pylovo.classification.clustering.get_no_clusters_for_clustering import get_no_clusters_for_clustering
   from pylovo.classification.clustering.apply_clustering_for_visualisation import apply_clustering_for_visualisation
   from pylovo.classification.clustering.report_grid_counts import report_grid_counts

   removed = prepare_data_for_clustering(sample_from_postcode_result_only=True)  # steps 1-4, returns the PLZ left out
   parameters = get_parameters_for_clustering(additional_filtering=False)        # step 6a: list of column names
   best = get_no_clusters_for_clustering(parameters, additional_filtering=False) # step 6b: algorithm, no_clusters, ch_index
   apply_clustering_for_visualisation(                                           # step 7, replaces an existing result
       list_of_clustering_parameters=parameters, n_clusters_kmeans=5, n_clusters_gmm=4, additional_filtering=False)
   report_grid_counts(additional_filtering=False, plz_without_grids=removed)     # the summary

``DatabaseCommunication.get_clustering_parameters_for_classification_version(additional_filtering)`` returns the grids
that enter the clustering. ``kmeans_clustering`` and ``gmm_tied_clustering`` in
``pylovo.classification.clustering.clustering_algorithms`` cluster any data frame with the key figures and return the
clustered frame and the representative grids.

Repeat or delete a classification
---------------------------------

To cluster again with other parameters, numbers of clusters or filters under the same id, run steps 6 and 7 once more
from Python (see above). Step 7 replaces the rows of the classification in one transaction. ``pylovo-classify``
itself always starts a new classification and rejects an existing id.

.. code-block:: bash

   uv run pylovo-delete classification --version 1

removes the classification with its sample set and its result (``ON DELETE CASCADE``) and refreshes the
materialized view ``transformer_classified_with_grid``. The id is the integer ``CLASSIFICATION_VERSION``, and an
unknown id is reported. The grids and their key figures stay.

Things to know
--------------

* The sample is reproducible (seed and sorted register, see step 1). ``sample_set`` remains the record of it.
* The filters are applied when the grids are read and nothing is stored. The notebooks and the Python functions use
  ``ADDITIONAL_FILTERING`` of ``config_clustering.yaml``, which holds the choice of the last ``pylovo-classify`` run:
  set it to the value of the classification you look at.
* Step 7 can be repeated per classification id (see above).
* The lower filter thresholds come from grids of pylovo 0.8.0 and are not re-derived for the current generator
  (:ref:`classification-thresholds`).

.. _classification-thresholds:

The lower filter thresholds
~~~~~~~~~~~~~~~~~~~~~~~~~~~

The shipped values (0.115, 14.332, 0.258 and 35.316) are the means of ``avg_trafo_dis``, ``no_house_connections``,
``vsw_per_branch`` and ``no_households`` over k-means cluster 0 of one classification over 100 PLZ. That
classification clustered with exactly these four parameters and without additional filtering, on grids of pylovo 0.8.0.
Cluster 0 is the cluster whose representative grid has the fewest households, the small filling grids. The generator
has changed since (transformer sizing, cable catalogue, connection aggregation) and those grids are not stored, so a
classification of today's grids gives other values. To derive them again, classify with the four parameters and
without additional filtering and run the helper on that classification:

.. code-block:: bash

   uv run --extra plots pylovo-classify --no-additional-filtering --restrict-to-generated \
       --manual --parameters avg_trafo_dis,no_house_connections,vsw_per_branch,no_households --clusters 4,4
   uv run --extra plots python -m pylovo.classification.utils.get_average_values_clustering_parameters \
       --classification-id 2

It prints the four means as ``THRESHOLD_*`` lines for ``config_clustering.yaml`` and notes when
``LIST_OF_CLUSTERING_PARAMETERS`` of the configuration is another list, because cluster 0 depends on the clustering
parameters. An earlier version of the helper averaged ``LIST_OF_CLUSTERING_PARAMETERS`` (three parameters in the
shipped file, without ``no_households``) over cluster 0 of all classifications and could not give the shipped values.

Troubleshooting
---------------

.. list-table::
   :header-rows: 1
   :widths: 38 62
   :class: fixed-table

   * - Message or symptom
     - Cause and remedy
   * - ``pylovo-classify needs the plotting packages of the 'plots' extra``
     - Install it: ``uv sync --extra plots``, or start the command with ``uv run --extra plots pylovo-classify``.
   * - ``Classification version N already exists. Set a new CLASSIFICATION_VERSION in ...``
     - Set a new id in ``config_classification.yaml`` or run ``pylovo-delete classification --version N``.
   * - ``Generation parameters differ from the stored snapshot``
     - The generation configuration changed since ``VERSION_ID`` was created (:ref:`versions`).
   * - ``RegioStaR class <c>: <n> PLZ requested but only <m> available``
     - A class quota exceeds the PLZ of that class. Lower ``N_SAMPLES`` or widen the pool. Nothing was stored.
   * - ``Automatic parameter selection failed for <n> grids and <k> parameters``
     - Too few or degenerate grids, or a missing value in a key figure. Use a larger sample or enter parameters yourself.
   * - ``KeyError`` in step 7
     - A name in ``LIST_OF_CLUSTERING_PARAMETERS`` is not a column of ``clustering_parameters``.
   * - Fewer grids than expected
     - Read the summary at the end of the run: it names the PLZ without grids or without PLZ analysis and the grids
       whose calculation failed (warnings, details in ``log/log.txt``) and counts the grids per filter.
   * - ``pylovo-classify: Unknown parameter(s): ...`` or ``All values must be in NO_OF_CLUSTERS_ALLOWED``
     - A value of ``--parameters`` or ``--clusters`` is wrong. The command checks them before anything runs.
