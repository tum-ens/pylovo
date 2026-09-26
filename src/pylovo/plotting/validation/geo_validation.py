"""
Geographic validation plotting functions.

This module contains functions for visualizing grid geometries and assets
on geographic maps using pandapower's Plotly map plots.

Note:
    With pandapower 3.4, ``on_map=True`` plots can fail for German grids with
    ``AttributeError: 'NoneType' object has no attribute 'address'``: pandapower's
    geo-coordinate check passes (lon, lat) to a geopy reverse lookup that expects
    (lat, lon) and needs internet access.
"""

import json
from pathlib import Path
from typing import Optional, Any

import numpy as np
import pandapower as pp
import plotly
import pandapower.plotting.plotly as pp_plotly
from pandapower.plotting.plotly import vlevel_plotly
from pandapower.plotting.plotly.mapbox_plot import set_mapbox_token

from pylovo.config_loader import ACCESS_TOKEN_PLOTLY, PLOT_COLOR_DICT, RESULT_DIR, VERSION_ID
from pylovo.database.database_client import DatabaseClient
from pylovo.plotting.utils import pandapower_on_map

if ACCESS_TOKEN_PLOTLY:
    set_mapbox_token(ACCESS_TOKEN_PLOTLY)


def plot_trafo_on_map(plz: int, save_plots: bool = False) -> Any:
    """Plot the transformers of a PLZ on a map, colored by their size.

    Colors come from ``PLOT_COLOR_DICT`` in ``config_analysis.yaml``; if a size is
    missing there, pandapower's default colors are used for all sizes.

    Args:
        plz: Postal code.
        save_plots: Also save the figure as ``trafo_on_map.html`` under
            ``RESULT_DIR/figures/version_<VERSION_ID>/<plz>``.

    Returns:
        plotly.graph_objects.Figure: The map figure.
    """
    net_plot = pp.create_empty_network()
    with DatabaseClient() as dbc_client:
        cluster_list = dbc_client.get_list_from_plz(plz)
        nets = []
        for kcid, bcid in cluster_list:
            try:
                nets.append(dbc_client.read_net_db(plz, kcid, bcid))
            except Exception:
                continue

    grid_index = 1
    for net in nets:
        for row in net.trafo[["sn_mva", "lv_bus"]].itertuples():
            trafo_size = round(row.sn_mva * 1e3)

            # Extract bus coordinates
            trafo_geom = None
            if "geo" in net.bus.columns and row.lv_bus in net.bus.index:
                geo_str = net.bus.at[row.lv_bus, "geo"]
                if geo_str and isinstance(geo_str, str):
                    try:
                        geo_data = json.loads(geo_str)
                        coords = geo_data.get("coordinates", [])
                        if len(coords) == 2:
                            trafo_geom = np.array([coords[0], coords[1]])
                    except (json.JSONDecodeError, ValueError):
                        pass
            elif hasattr(net, 'bus_geodata') and row.lv_bus in net.bus_geodata.index:
                trafo_geom = np.array(net.bus_geodata.loc[row.lv_bus, ["x", "y"]])

            if trafo_geom is not None:
                # vn_kv carries the transformer size so that vlevel_plotly colors by size
                pp.create_bus(
                    net_plot,
                    name=f"Distribution_grid_{grid_index}<br>transformer: {trafo_size}_kVA",
                    vn_kv=trafo_size,
                    geodata=tuple(trafo_geom),
                    type="b",
                )
            grid_index += 1

    # vlevel_plotly needs a color for every size; otherwise fall back to its defaults
    sizes = set(net_plot.bus["vn_kv"])
    colors_dict = PLOT_COLOR_DICT if sizes.issubset(PLOT_COLOR_DICT) else None
    # The bus geodata are WGS84 already; no projection argument (it only triggers a no-op convert_crs).
    with pandapower_on_map():
        figure = vlevel_plotly(net_plot, on_map=True, colors_dict=colors_dict)

    if save_plots:
        savepath_folder = Path(RESULT_DIR, "figures", f"version_{VERSION_ID}", str(plz))
        savepath_folder.mkdir(parents=True, exist_ok=True)
        savepath_file = Path(savepath_folder, "trafo_on_map.html")
        plotly.offline.plot(figure, filename=str(savepath_file))

    return figure


def plot_grid_on_map_plotly(
    plz: int,
    kcid: int,
    bcid: int,
    title: Optional[str] = None
) -> Any:
    """Visualize a single grid on a map using ``pandapower.plotting.plotly.simple_plotly``.

    Args:
        plz: Postal code.
        kcid: K-means cluster ID.
        bcid: Building cluster ID.
        title: Title of the plot.

    Returns:
        plotly.graph_objects.Figure | None: The figure, or ``None`` if the grid
        could not be loaded or plotted (the reason is printed).
    """
    try:
        with DatabaseClient() as dbc_client:
            net = dbc_client.read_net_db(plz, kcid, bcid)
    except Exception as e:
        print(f"Could not load grid {kcid}-{bcid}: {e}")
        return None

    # on_map=True enables the map background
    try:
        with pandapower_on_map():
            fig = pp_plotly.simple_plotly(net, on_map=True)
        if title:
            fig.update_layout(title_text=title)
        return fig

    except Exception as e:
        print(f"Plotting failed: {e}")
        return None
