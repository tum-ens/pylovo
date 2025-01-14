# Grids for the indicated PLZ are generated
# can also be used for PLZ areas that are not part of the official municipal register and have been created by the user

import time

from pylovo.GridGenerator import GridGenerator

# enter a plz to generate grid for:
# plz = "99999"  # test muc
# plz = "10000"  # test forchheim middle
# plz = "10001"  # test small
plz = '10002'  # test large
# plz = '10003'  # small within test large

# timing of the script
start_time = time.time()

# generate grid
gg = GridGenerator(plz=plz)
gg.generate_grid()
gg.analyse_results()

# end timing
print("--- %s seconds ---" % (time.time() - start_time))
