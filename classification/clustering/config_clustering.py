# CLASSIFICATION VERSION 1
# set clustering parameters
# param1 = 'no_branches'
# param2 = 'avg_trafo_dis'
# param3 = 'max_no_of_households_of_a_branch'
# param4 = 'no_house_connections_per_branch'
# LIST_OF_CLUSTERING_PARAMETERS = [param1, param2, param3, param4]
#
# # set number of clusters
# N_CLUSTERS_KMEDOID = 5
# N_CLUSTERS_KMEANS = 5
# N_CLUSTERS_GMM = 4  # refers to gmm tied
# -------------------------------------------------------------------------------------------
# # CLASSIFICATION VERSION 3
# # set clustering parameters
# param1 = 'no_branches'
# param2 = 'avg_trafo_dis'
# param3 = 'max_no_of_households_of_a_branch'
# LIST_OF_CLUSTERING_PARAMETERS = [param1, param2, param3]
#
# # set number of clusters
# N_CLUSTERS_KMEDOID = 3
# N_CLUSTERS_KMEANS = 6
# N_CLUSTERS_GMM = 5  # refers to gmm tied
#
# # set threshold values
# THRESHOLD_MAX_TRAFO_DIS = 2.5  # cables between trafo and house connection too long
# THRESHOLD_HOUSEHOLDS_PER_BUILDING = 150  # buildings wrongly attributed as residential
# -------------------------------------------------------------------------------------------
# CLASSIFICATION VERSION 4
# set clustering parameters
param1 = 'avg_trafo_dis'
param2 = 'no_house_connections'
param3 = 'vsw_per_branch'
param4 = 'no_households'
LIST_OF_CLUSTERING_PARAMETERS = [param1, param2, param3, param4]

# set number of clusters
N_CLUSTERS_KMEDOID = 5
N_CLUSTERS_KMEANS = 7
N_CLUSTERS_GMM = 7  # refers to gmm tied

# set threshold values
THRESHOLD_MAX_TRAFO_DIS = 2.5  # cables between trafo and house connection too long
THRESHOLD_HOUSEHOLDS_PER_BUILDING = 150  # buildings wrongly attributed as residential
