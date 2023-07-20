from syngrid.GridGenerator import GridGenerator

plz = "91301"  # forchheim

gg = GridGenerator(plz=plz)
pg = gg.pgr
pg.delete_transformers()
