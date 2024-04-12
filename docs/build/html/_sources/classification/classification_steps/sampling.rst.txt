Create a Sample Set of PLZ for Clustering
==========================================

Motivation
----------

To capture the diversity of different types of settlements and represent them for the chosen region adequately a
sampling algorithm is developed. For the area of interest that could be a Bundesland or Germany generating grids for
the whole region takes to long. The number of PLZ is reduced from 8181 (Germany) or e.g. 2065 (Bayern) to 100 PLZ in
the representative sample set.

Usage
-----

The sample set is created with the function:

.. autofunction:: classification.sampling.sample.create_sample_set

The result can be retrieved from the database as a DataFrame with

.. autofunction:: classification.sampling.sample.get_sample_set

Joining Regiostar with the Municipal Register
----------------------------------------------

In order to sample areas from the Regiostar Classes they need to be mapped with the Municipal Register
(Gemeindeverzeichnis) from Germany. You can refer to :doc:`../../docs_pylovo/municipal_register/municipal_register`
for more details.

Sampling Algorithm
--------------------

To make sure all settlement types are represented in the sample set the Regio 7 Dataset with seven different types of
municipalities is chosen.

.. image:: ../../images/classification/regiostar.png
    :width: 600
    :alt: Default view

The share of PLZ samples from each RegioStar 7 class corresponds to the  population share of the class of the total
population. (An analysis by the Chair of Renewable and
Sustainable Energy Systems at TUM have shown that the percentage of energy usage of a Regio7 class corresponds to its
population share.)

For Bayern the distribution of samples are:

.. image:: ../../images/classification/anz_samples.png
    :width: 400
    :alt: Default view

Within the classes the population density distribution of the class is reproduced.

.. image:: ../../images/classification/pop_den.png
    :width: 600
    :alt: Default view
