"""Stage 1 of the LV generation: the inputs of a postcode in its temporary tables.

:class:`InputPreparation` reads the postcode, the buildings and the streets from the InfDB, derives the loads
and the settlement type, adds the candidate transformers and builds the routable street network.
"""

from pylovo import postcode_border
from pylovo.config_loader import (
    AGGREGATE_NEARBY_CONNECTION_POINTS,
    CONFIG_EQUIPMENT_DATA,
    CONNECTION_POINT_AGGREGATION_MAX_BUILDINGS,
    CONNECTION_POINT_AGGREGATION_RADIUS_M,
    CONSUMER_CATEGORIES,
    EXCLUDE_BUILDINGS_WITHOUT_ADDRESS,
    RESIDENTIAL_ONLY_GENERATION,
    RURAL_MAX_HOUSEHOLDS,
    RURAL_MIN_BUILDING_DISTANCE,
    TARGET_EPSG,
    URBAN_MAX_BUILDING_DISTANCE,
    URBAN_MIN_HOUSEHOLDS,
    USE_DSO_TRANSFORMER_POSITIONS,
    USE_MANUAL_TRANSFORMER_POSITIONS,
    USE_OPEN_TRANSFORMER_POSITIONS,
)
from pylovo.infdb.infdb_client import address_filter_warning
from pylovo.lv_grid.context import Stage


class InputPreparation(Stage):
    """Fill the temporary tables of the postcode: :meth:`run` calls the ``prepare_*`` steps in order."""

    def run(self) -> None:
        """Prepare all inputs of the postcode."""
        self.prepare_lod2_transformer_stations()
        self.prepare_data_from_config()
        self.prepare_postcodes()
        self.prepare_buildings()
        self.prepare_transformers()
        self.prepare_ways()

    def prepare_lod2_transformer_stations(self):
        """Add the LoD2 transformer-station buildings of the PLZ to the shared transformer candidates.

        Only when open or manual positions are used: a run without them does not use the candidates. ``pylovo.transformers`` is shared by all PLZ, so the rows are written and
        committed in a short transaction of their own before the PLZ is generated (the long PLZ
        transaction would hold their row locks for minutes and could deadlock with a neighbouring PLZ).
        """
        if not (USE_OPEN_TRANSFORMER_POSITIONS or USE_MANUAL_TRANSFORMER_POSITIONS):
            return
        transformer_station_buildings = self.inf_dbc.fetch_transformer_station_buildings_from_infdb(self.plz)
        transformer_candidate_count = self.dbc.upsert_lod2_transformer_stations(transformer_station_buildings)
        self.dbc.commit_changes()
        if transformer_candidate_count:
            self.logger.info(
                f"LoD2 transformer-station buildings added to transformer candidates: {transformer_candidate_count}"
            )

    def prepare_data_from_config(self):
        """
        Load data from config.
        """
        self.dbc.insert_equipment_data_from_config(equipment_data=CONFIG_EQUIPMENT_DATA)
        self.dbc.commit_changes()
        self.dbc.insert_consumer_categories_from_config(consumer_categories=CONSUMER_CATEGORIES)

    def prepare_postcodes(self):
        """
        Caches postcode from raw data tables and stores in temporary tables.
        FROM: postcode (local) or InfDB opendata.postcodes_germany
        INTO: postcode_result

        The local postcode table may be empty or stale for new PLZ
        regions added after the initial pylovo-setup run.  To avoid requiring a full
        re-run of setup, the postcode geometry is fetched on-demand from InfDB and
        inserted into the local postcode table before copying to postcode_result.
        """
        if not self.dbc.postcode_exists_locally(self.plz):
            postcode_row = self.inf_dbc.fetch_postcode_from_infdb(self.plz)
            if postcode_row is None:
                raise ValueError(
                    f"PLZ {self.plz} not found in InfDB opendata.postcodes_germany. "
                    "Cannot proceed without postcode geometry."
                )
            self.dbc.insert_postcode(postcode_row)
            self.logger.info(f"Missing postcode for plz {self.plz} fetched from InfDB and inserted into local database.")
        self.dbc.copy_postcode_result_table(self.plz)
        self.logger.info(f"Starting grid generation for plz {self.plz}")

    def prepare_buildings(self):
        """
        Caches buildings from raw data tables and stores in temporary tables.
        FROM: the InfDB buildings
        INTO: buildings_tem
        """
        warning = address_filter_warning(EXCLUDE_BUILDINGS_WITHOUT_ADDRESS, self.inf_dbc.has_auxiliary_class())
        if warning:
            self.logger.warning(warning)
        buildings_data = self.inf_dbc.fetch_buildings_from_infdb(self.plz)
        self.dbc.set_buildings_table(buildings_data)
        if RESIDENTIAL_ONLY_GENERATION:
            removed_non_residential = self.dbc.remove_non_residential_buildings_from_buildings_tem()
            self.logger.info(
                f"Residential-only generation enabled: removed {removed_non_residential} non-residential buildings"
            )
        self.logger.info("Buildings_tem table prepared")
        self.dbc.remove_duplicate_buildings()
        self.logger.info("Duplicate buildings removed from buildings_tem")

        # Fill missing household counts before they contribute to the postcode's
        # settlement classification, then calculate residential demand.
        unloadcount = self.dbc.set_building_peak_load()
        self.logger.info(
            f"Building peakload calculated in buildings_tem, {unloadcount} unloaded buildings are removed from "
            f"buildings_tem"
        )

        # The MV-direct decision comes before the removal of the non-residential buildings with a
        # transformer candidate inside: such a building leaves the LV input either way, but a large one
        # (a customer with its own substation) is stored as MV customer first. The removed buildings do
        # not count in the settlement classification, as before.
        too_large_consumers = self.dbc.update_too_large_consumers_to_zero()
        self.logger.debug(
            f"{too_large_consumers} non-residential components assumed MV-direct and excluded from LV modeling"
        )
        # the connection level of every building (CONNECTION_LEVEL_THRESHOLDS_KW), before the MV customers are stored
        self.dbc.set_connection_levels()
        if too_large_consumers:
            saved = self.dbc.save_mv_customers(self.plz)
            self.logger.info(f"{saved} MV customers stored in pylovo.mv_customers")
        removed_transformer_buildings = self.dbc.remove_non_residential_buildings_overlapping_transformers(
            include_dso=USE_DSO_TRANSFORMER_POSITIONS)
        if removed_transformer_buildings:
            self.logger.info(
                f"Removed {removed_transformer_buildings} non-residential buildings overlapping transformer candidates"
            )

        # A metric that cannot be calculated (no residential building with households, fewer than two
        # buildings) stays NULL and counts as rural, so every PLZ gets a settlement type.
        avg_hh = house_dist = None
        try:
            avg_hh = self.dbc.calculate_avg_households_per_building(self.plz)
        except ValueError as e:
            self.logger.warning(f"{e} The household metric counts as rural.")
        try:
            house_dist = self.dbc.calculate_house_distance_metric(self.plz)
        except ValueError as e:
            self.logger.warning(f"{e} The distance metric counts as rural.")
        settlement_type = self.dbc.set_settlement_type_per_plz(self.plz, settlement_type_thresholds=
        {"rural_max_households": RURAL_MAX_HOUSEHOLDS,
         "urban_min_households": URBAN_MIN_HOUSEHOLDS,
         "rural_min_distance": RURAL_MIN_BUILDING_DISTANCE,
         "urban_max_distance": URBAN_MAX_BUILDING_DISTANCE})
        self.logger.info(
            f"Settlement type determined (avg_households_per_building={None if avg_hh is None else round(avg_hh, 2)}, "
            f"house_distance={None if house_dist is None else round(house_dist, 1)} m, settlement_type={settlement_type})"
        )

    def prepare_transformers(self):
        """
        Cache transformers from raw data tables and stores in temporary tables.
        FROM: transformers
        INTO: buildings_tem
        """
        self.dbc.set_buildings_tem_plz(self.plz)
        sources = {"include_dso": USE_DSO_TRANSFORMER_POSITIONS, "include_open": USE_OPEN_TRANSFORMER_POSITIONS,
                   "include_manual": USE_MANUAL_TRANSFORMER_POSITIONS}
        if any(sources.values()):
            self.dbc.insert_transformers(self.plz, **sources)
            self.logger.info(
                "Transformers inserted into buildings_tem table "
                f"(dso={USE_DSO_TRANSFORMER_POSITIONS}, open={USE_OPEN_TRANSFORMER_POSITIONS}, "
                f"manual={USE_MANUAL_TRANSFORMER_POSITIONS})"
            )
        else:
            self.logger.info("Existing transformer positions disabled by configuration")
        removed_transformer_buildings = self.dbc.remove_transformer_evidence_buildings_from_buildings_tem(**sources)
        self.logger.info(
            f"Removed {removed_transformer_buildings} transformer-evidence buildings from buildings_tem consumer input"
        )

    def prepare_ways(self):
        """
        Cache ways, create network, connect buildings to the ways network
        FROM: the InfDB street tables, ``buildings_tem``
        INTO: ``ways_tem``, ``buildings_tem``, ``ways_tem_vertices_pgr``
        """
        ways_rows = self.inf_dbc.fetch_ways_from_infdb(self.plz)
        if postcode_border.enabled():
            width_m = postcode_border.neighboring_street_width_m()
            neighboring_rows = self.inf_dbc.fetch_neighboring_ways(
                self.plz, self.dbc.get_postcode_polygon_wkb(self.plz), TARGET_EPSG,
                width_m, first_way_id=max(int(row[-1]) for row in ways_rows))
            self.logger.info(
                f"Neighboring streets: {len(neighboring_rows)} ways of other PLZ within {width_m:.0f} m added")
            ways_rows = list(ways_rows) + list(neighboring_rows)
        ways_count = self.dbc.set_ways_tem_table_infdb(ways_rows)
        self.logger.info(f"The ways_tem table filled with {ways_count} ways")

        # Index loaded roads before nearest-road searches.
        self.dbc.index_and_analyze_staging(self.plz)

        # Run preprocessing functions that segment roads and connect buildings
        self.dbc.preprocess_ways()
        self.logger.info("Ways preprocessing completed in ways_tem.")

        # Build pgRouting topology on the processed network
        self.dbc.build_pgr_network_topology(self.plz)
        self.logger.info("pgRouting network topology created from ways_tem.")

        self.dbc.update_ways_cost()
        unconn = self.dbc.set_vertice_id()
        self.logger.debug(f"vertice id set, {unconn} buildings with no vertice id")
        if AGGREGATE_NEARBY_CONNECTION_POINTS:
            aggregated_rows = self.dbc.aggregate_nearby_connection_points(
                CONNECTION_POINT_AGGREGATION_RADIUS_M,
                CONNECTION_POINT_AGGREGATION_MAX_BUILDINGS,
            )
            self.logger.info(
                "Aggregated nearby building connection points "
                f"for {aggregated_rows} building rows "
                f"(radius={CONNECTION_POINT_AGGREGATION_RADIUS_M} m, "
                f"max_buildings={CONNECTION_POINT_AGGREGATION_MAX_BUILDINGS})"
            )
