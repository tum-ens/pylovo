from syngrid.GridGenerator import GridGenerator

plz = "91301"
gg = GridGenerator(plz=plz)
gg.generate_grid()
gg.analyse_results()
gg.plot_results()
