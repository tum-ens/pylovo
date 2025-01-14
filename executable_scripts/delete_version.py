from pylovo.GridGenerator import GridGenerator

# select version you want to delete entirely
version_id = "1.0"

# delete networks
gg = GridGenerator(plz="91301") # just a dummy plz for the initialization of the class
gg.pgr.delete_version_from_all_tables(version_id=version_id)