from classification.parameter_calculation.GridParameters import GridParameters
from pylovo.GridGenerator import GridGenerator
from pylovo.config_version import VERSION_ID


class ParameterCalculator:
    def __init__(self, plz):
        self.plz = plz
        # connect to database
        self.gg = GridGenerator(plz=plz)
        self.pg = self.gg.pgr

    def calc_parameters_for_grids(self) -> None:
        """
        this function extracts and calculates parameters for each net in a plz
        """
        # check if for this version ID and PLZ the parameters have already been calculated
        parameter_count = self.pg.count_clustering_parameters(plz=self.plz)
        if parameter_count > 0:
            print(f"The parameters for the grids of postcode area {self.plz} and version {VERSION_ID} "
                  f"have already been calculated.")
            return
        # all nets within plz
        cluster_list = self.pg.get_list_from_plz(self.plz)
        # loop over all networks
        for kcid, bcid in cluster_list:
            gp = GridParameters(self.plz, bcid, kcid, self.pg)
            print(bcid, kcid)
            gp.calc_grid_parameters()
