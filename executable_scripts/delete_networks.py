from syngrid.GridGenerator import GridGenerator

# select plz and version you want to delete the networks for
plz = "91301"
version_id = "3"

# delete networks
gg = GridGenerator(plz=plz)
gg.pgr.delete_plz_from_all_tables(plz, version_id)
