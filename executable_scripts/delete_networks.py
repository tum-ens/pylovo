from pylovo.GridGenerator import GridGenerator

# select plz and version you want to delete the networks for
plz = "80803"
version_id = "1.0"

# delete networks
gg = GridGenerator(plz=plz)
gg.pgr.delete_plz_from_all_tables(plz, version_id)
