"""Voltage repair of the LV generation: a grid outside the voltage band is planned again.

:class:`VoltageRepair` finds what pulls a grid below the band (far feeder ends, long house connections) and
changes the supply areas accordingly: the far ends get stations of their own
(:class:`~pylovo.lv_grid.greenfield_areas.GreenfieldAreas`, :class:`~pylovo.lv_grid.station_planning.StationPlanner`),
and a house connection that is too long is planned as a feeder.
"""

import pandas as pd  # type: ignore

from pylovo import utils
from pylovo.config_loader import MAX_SERVICE_DESIGN_VOLTAGE_DROP_PERCENT, POWER_FLOW_MIN_VM_PU
from pylovo.electrical_backend import IElectricalBackend
from pylovo.lv_grid.context import GenerationContext, Stage
from pylovo.lv_grid.station_planning import StationPlanner


class VoltageRepair(Stage):
    """Plan a grid with a voltage violation again (:meth:`repair`)."""

    def __init__(self, context: GenerationContext):
        super().__init__(context)
        self.stations = StationPlanner(context)
        self.areas = self.stations.areas

    def targets(self, backend: IElectricalBackend, buildings_df: pd.DataFrame,
                ont_vertice: int) -> tuple[list[int], list[int]]:
        """What a voltage repair changes in a grid (after ``GridStorage.validate_net``).

        The feeders may use the band down to ``POWER_FLOW_MIN_VM_PU`` plus the share of the house
        cables (``MAX_SERVICE_DESIGN_VOLTAGE_DROP_PERCENT``).

        * Far feeder ends: the planning nodes whose street node (the station busbar for the station's
          own vertex) lies below that limit are too far out for the station. The far end of a feeder
          goes as a whole, also the nodes whose consumers are still within the band, so that a new
          station there gets the area such a station would supply. Returned only if a consumer below
          the band sits at such a node. A planning node without a bus of its own (a cabinet network)
          counts by its consumers' voltage.
        * House connections: a consumer below the band whose street node is within that limit is
          below it because of its own house cable (a large building far from the street). Its
          building becomes its own planning node, so that its connection line is planned and sized as
          a feeder. A building that is its own planning node already counts among the far feeder ends
          when it stays below the band; a new station for it then stands at the building.

        Returns:
            ``(far feeder ends, vertices of the buildings whose connection becomes a feeder)``.
        """
        node_limit = POWER_FLOW_MIN_VM_PU + MAX_SERVICE_DESIGN_VOLTAGE_DROP_PERCENT / 100
        far, triggered, connections = set(), False, set()
        for vertex, point in zip(buildings_df["vertice_id"].astype(int), utils.planning_nodes(buildings_df).astype(int)):
            consumer_low = backend.get_bus_voltage_pu(f"Consumer Nodebus {vertex}") < POWER_FLOW_MIN_VM_PU
            try:
                node_vm = backend.get_bus_voltage_pu("LVbus 1" if point == int(ont_vertice)
                                                     else f"Connection Nodebus {point}")
            except Exception:  # no bus at the street node
                node_vm = None
            if (node_vm < node_limit) if node_vm is not None else consumer_low:
                far.add(int(point))
                triggered = triggered or consumer_low
            elif consumer_low and point != vertex:
                connections.add(int(vertex))
        return (sorted(far) if triggered else []), sorted(connections)

    def repair(self, kcid: int, bcid: int, backend: IElectricalBackend, buildings_df: pd.DataFrame,
               ont_vertice: int) -> list[tuple[int, int]]:
        """Plan a grid again whose validation power flow left the voltage band (``MAX_VOLTAGE_REPAIRS``).

        :meth:`targets` gives what changes. A building below the band because of its own house
        cable becomes its own planning node: its connection line is then a feeder section (sized for
        the feeder share, with parallel cables up to ``MAX_VOLTAGE_PARALLEL_FEEDER_CABLES``) and the
        grid is built again. The buildings at the far feeder ends are too far out for the station:

        * an existing station (``bcid < 0``) keeps its other buildings, and an unrated one gets the
          smallest catalogue rating above its new load again; with no building left, its grid is deleted;
        * a greenfield grid gives up all its buildings (unless its only planning node is the station's
          own vertex: neither a split nor another station would help).

        The released buildings are clustered like the unassigned buildings of a kcid
        (``GreenfieldAreas.split``, without the merge of small areas): those of a station into
        as few areas as the load and distance limits allow, those of a greenfield grid into at least two.
        The new areas get bcids behind the existing ones, station positions and ratings. An area of a
        single building that is its own planning node gets its station at the building, so a building
        that its feeder cannot supply within the band ends with a station of its own.

        Args:
            kcid: K-means cluster ID.
            bcid: Building cluster ID of the grid.
            backend: Backend holding the solved validation power flow of the grid.
            buildings_df: Buildings of the grid.
            ont_vertice: Station vertex of the grid.

        Returns:
            The grids to build (again): the grid itself if it is kept, and the new ones; empty if no
            repair applies.
        """
        points, connections = self.targets(backend, buildings_df, ont_vertice)
        all_points = utils.planning_nodes(buildings_df).astype(int).unique()
        if bcid >= 0 and len(all_points) < 2 and int(all_points[0]) == int(ont_vertice):
            points = []
        if not points and not connections:
            return []
        if connections:
            self.dbc.route_connections_as_feeders(self.plz, kcid, bcid, connections)
        if not points:
            self.logger.info(
                f"Voltage repair for PLZ {self.plz}, kcid={kcid}, bcid={bcid}: the connection lines of "
                f"{len(connections)} buildings below the band because of their house cables are planned as feeders")
            return [(kcid, bcid)]
        rebuild = []
        if bcid < 0:
            released = self.dbc.release_buildings(self.plz, kcid, bcid, points)
            if len(points) == len(all_points):
                self.dbc.delete_grid(self.plz, kcid, bcid)
            else:
                rebuild.append((kcid, bcid))
                if int(ont_vertice) not in self.dbc.get_brownfield_transformer_capacity_map([int(ont_vertice)]):
                    capacities, _ = self.dbc.get_transformer_data(self.dbc.get_settlement_type_from_plz(self.plz))
                    kept = buildings_df[~utils.planning_nodes(buildings_df).astype(int).isin(points)]
                    load = utils.planned_station_load(utils.simultaneous_peak_load(
                        kept, self.consumer_categories, utils.planning_nodes(kept).astype(int).unique().tolist()))
                    rating = utils.smallest_rating_above(capacities, load)
                    self.dbc._set_transformer_rated_power(
                        self.plz, kcid, bcid, rating if rating is not None else int(capacities.max()))
        else:
            released = self.dbc.release_buildings(self.plz, kcid, bcid)
            self.dbc.delete_grid(self.plz, kcid, bcid)
        new_bcids = self.areas.split(
            self.plz, kcid, first_bcid=self.dbc.get_next_greenfield_bcid(self.plz, kcid), merge=False, replace=False,
            first_cut=1 if bcid < 0 else 2)
        for new_bcid in new_bcids:
            self.stations.place_station(self.plz, kcid, new_bcid)
            self.dbc.update_transformer_rated_power(self.plz, kcid, new_bcid)
        rebuild += [(kcid, new_bcid) for new_bcid in new_bcids]
        self.logger.info(
            f"Voltage repair for PLZ {self.plz}, kcid={kcid}, bcid={bcid}: {len(points)} of {len(all_points)} "
            f"planning nodes beyond the feeder share of the voltage band, {released} buildings released"
            f"{' (the station keeps the others)' if bcid < 0 and (kcid, bcid) in rebuild else ''}"
            f"{f', the connection lines of {len(connections)} buildings planned as feeders' if connections else ''}; "
            f"new greenfield grids {new_bcids}")
        return rebuild
