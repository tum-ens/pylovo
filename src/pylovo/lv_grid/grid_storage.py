"""Validation and storage of one built grid of the LV generation.

:class:`GridStorage` runs the validation power flow of a finished network and stores the network with its
power-flow status and planning diagnostics.
"""

from pathlib import Path

from pylovo.config_loader import (
    ELECTRICAL_BACKEND,
    LV_REFERENCE_VOLTAGE_PU,
    POWER_FLOW_MAX_VM_PU,
    POWER_FLOW_MIN_VM_PU,
    RESULT_DIR,
    SAVE_GRID_FOLDER,
    VERSION_ID,
)

from pylovo.electrical_backend import IElectricalBackend
from pylovo.station_voltage import validate_operating_point

from pylovo.lv_grid.context import Stage


class GridStorage(Stage):
    """Validate a built grid (:meth:`validate_net`) and store it (:meth:`save_net`)."""

    def validate_net(self, backend: IElectricalBackend, kcid, bcid) -> tuple[str, dict[str, float | None]]:
        """Run the validation power flow of a finished grid.

        Runs the power flow of the snapshot loads with the station voltage of
        :mod:`pylovo.station_voltage` (LV busbar at ``LV_REFERENCE_VOLTAGE_PU``) and classifies it as
        ``converged``, ``voltage_violation`` (outside ``POWER_FLOW_VOLTAGE_LIMITS``) or
        ``not_converged``. The backend keeps the solved net.

        Args:
            backend: Backend holding the finished network.
            kcid: K-means cluster ID
            bcid: Building cluster ID

        Returns:
            ``(power-flow status, voltage-drop diagnostics)``; the diagnostics are the largest feeder,
            service and total LV voltage drops in p.u. (``None`` unless the pandapower power flow converged).
        """
        self.logger.debug(
            "Running synthetic transformer-coincident validation operating point "
            f"for kcid={kcid}, bcid={bcid}."
        )
        validation = validate_operating_point(
            backend, POWER_FLOW_MIN_VM_PU, POWER_FLOW_MAX_VM_PU, LV_REFERENCE_VOLTAGE_PU, logger=self.logger
        )
        station = validation.station
        if station is not None and station.applied:
            self.logger.debug(f"Station voltage for kcid={kcid}, bcid={bcid}: {station.describe()}")
        if validation.error is not None:
            self.logger.warning(
                "Synthetic transformer-coincident validation power flow failed "
                f"for kcid={kcid}, bcid={bcid}: {validation.error}"
            )
        elif validation.status == "voltage_violation":
            self.logger.warning(
                "Synthetic transformer-coincident validation power flow converged "
                f"but violated the voltage band for kcid={kcid}, bcid={bcid} "
                f"(min_vm_pu={validation.min_voltage_pu}, max_vm_pu={validation.max_voltage_pu}, "
                f"allowed=[{POWER_FLOW_MIN_VM_PU}, {POWER_FLOW_MAX_VM_PU}])."
            )
        elif validation.status == "converged":
            self.logger.info(
                "Synthetic transformer-coincident validation power flow converged "
                f"for kcid={kcid}, bcid={bcid}"
            )
        else:
            self.logger.warning(
                "Synthetic transformer-coincident validation power flow did NOT converge "
                f"for kcid={kcid}, bcid={bcid}"
            )
        return validation.status, validation.drops

    def save_net(
        self,
        backend: IElectricalBackend,
        kcid,
        bcid,
        planning_diagnostics: dict[str, float | bool | int] | None = None,
        validation: tuple[str, dict[str, float | None]] | None = None,
    ) -> str:
        """
        Validate the synthetic transformer-coincident operating point and save the grid.

        Runs the validation power flow (:meth:`validate_net`) unless its result is given, and
        stores the network JSON with the planning and voltage-drop diagnostics in
        ``grid_result`` (plus the SQL network tables for pandapower, and a JSON file if
        ``SAVE_GRID_FOLDER``). A grid is stored even if the power flow fails.

        Args:
            backend: Backend holding the finished network.
            kcid: K-means cluster ID
            bcid: Building cluster ID
            planning_diagnostics: Feeder and service planning diagnostics.
            validation: Result of :meth:`validate_net` for this backend, if it ran already.

        Returns:
            The power-flow status.
        """
        if planning_diagnostics is None:
            planning_diagnostics = {
                "ampacity_max_feeder_voltage_drop_percent": None,
                "selected_max_feeder_voltage_drop_percent": None,
                "feeder_voltage_drop_limit_met": None,
                "ampacity_max_service_voltage_drop_percent": None,
                "selected_max_service_voltage_drop_percent": None,
                "service_voltage_drop_limit_met": None,
                "service_voltage_upgraded_count": None,
                "long_service_connection_count": None,
                "max_total_design_voltage_drop_percent": None,
            }
        powerflow_status, voltage_drop_diagnostics = (
            validation if validation is not None else self.validate_net(backend, kcid, bcid))

        if powerflow_status != "converged":
            self.logger.warning(
                f"Grid with kcid:{kcid} bcid:{bcid} will be stored with status={powerflow_status}."
            )

        if SAVE_GRID_FOLDER:
            savepath_folder = Path(RESULT_DIR, "grids", f"version_{VERSION_ID}", str(self.plz))
            savepath_folder.mkdir(parents=True, exist_ok=True)
            filename = f"kcid{kcid}bcid{bcid}.json"
            savepath_file = Path(savepath_folder, filename)
            try:
                backend.export_to_format(filename=savepath_file)
            except Exception as e:
                self.logger.warning(
                    f"Failed to export grid file for kcid={kcid}, bcid={bcid}, status={powerflow_status}: {e}"
                )

        json_string = None
        try:
            json_string = backend.export_to_format(filename=None)
        except Exception as e:
            self.logger.warning(
                f"Failed to export grid JSON for kcid={kcid}, bcid={bcid}, status={powerflow_status}: {e}"
            )

        if ELECTRICAL_BACKEND == "pandapower":
            transformer_description = backend.net.trafo.name[0]
        else:
            transformer_description = "N/A"

        self.dbc.save_pp_net_with_json(
            self.plz,
            kcid,
            bcid,
            json_string,
            transformer_description,
            powerflow_status,
            **planning_diagnostics,
            **voltage_drop_diagnostics,
        )

        if ELECTRICAL_BACKEND == "pandapower":
            if json_string is None:
                self.logger.warning(
                    f"Skipping SQL net persistence for kcid={kcid}, bcid={bcid} because JSON export failed."
                )
            elif getattr(backend, "net", None) is None:
                self.logger.warning(
                    f"Skipping SQL net persistence for kcid={kcid}, bcid={bcid} because no backend network is present."
                )
            else:
                try:
                    self.dbc.save_pandapower_net_with_sql(
                        plz=self.plz,
                        kcid=kcid,
                        bcid=bcid,
                        net=backend.net,
                    )
                except Exception as e:
                    self.logger.warning(
                        f"Failed to store SQL net tables for kcid={kcid}, bcid={bcid}: {e}"
                    )

        self.logger.debug(
            f"Grid with kcid:{kcid} bcid:{bcid} is stored with status={powerflow_status}."
        )
        return powerflow_status
