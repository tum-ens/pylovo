# 7. cluster and write to transformer_classified
# --> clustered grids and representative grids
from classification.database_communication.DatabaseCommunication import DatabaseCommunication

dc = DatabaseCommunication()
df_parameters_of_grids = dc.municipal_register_with_clustering_parameters_for_classification_version()

dc.save_transformers_with_classification_info()
