"""Export grid geometries to GIS-friendly tables (used by ``pylovo-export`` and QGIS).

The pandapower ``bus`` and ``line`` tables are turned into GeoDataFrames: the GeoJSON in their
``geo`` columns becomes a shapely geometry (``None`` if missing or invalid), all other columns
are kept.
"""

import json
from typing import Tuple

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Point

from pylovo.database.database_client import DatabaseClient


def _geometry_from_geojson(geo, geometry_type):
    """Return ``geometry_type(coordinates)`` of a GeoJSON string, or None if it is missing or invalid."""
    if pd.isna(geo):
        return None
    try:
        return geometry_type(json.loads(geo)["coordinates"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def get_bus_line_geo_for_network(
    pandapower_net,
    plz: int,
    net_index: int = 0
) -> Tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Return the lines and buses of one pandapower network as GeoDataFrames.

    Args:
        pandapower_net: Network whose ``bus.geo`` / ``line.geo`` columns hold GeoJSON.
        plz: Postal code written to the ``plz`` column.
        net_index: Running number written to the ``net`` column (to tell grids apart).

    Returns:
        Tuple ``(line_geo, bus_geo)``. Both have the columns of the pandapower table plus
        ``net``, ``plz`` and a shapely ``geometry``; ``bus_geo`` also has ``consumer_bus``
        (True for buses named ``Consumer Nodebus``).
    """
    bus_table = pandapower_net.bus
    bus_geometries = [_geometry_from_geojson(row.get("geo"), Point) for _, row in bus_table.iterrows()]
    bus_geo = gpd.GeoDataFrame(bus_table.copy(), geometry=bus_geometries, crs="EPSG:4326")
    bus_geo['net'] = net_index
    bus_geo['consumer_bus'] = bus_geo['name'].str.contains("Consumer Nodebus")
    bus_geo['plz'] = plz

    line_table = pandapower_net.line
    line_geometries = [_geometry_from_geojson(row.get("geo"), LineString) for _, row in line_table.iterrows()]
    line_geo = gpd.GeoDataFrame(line_table.copy(), geometry=line_geometries, crs="EPSG:4326")
    line_geo['net'] = net_index
    line_geo['plz'] = plz

    return line_geo, bus_geo


def _concat(frames: list) -> gpd.GeoDataFrame:
    """Concatenate GeoDataFrames; an empty list gives an empty GeoDataFrame."""
    return pd.concat(frames) if frames else gpd.GeoDataFrame()


def get_bus_line_geo_for_plz(plz: int) -> Tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Return the lines and buses of all grids of a PLZ (``VERSION_ID`` of the config).

    Args:
        plz: Postal code.

    Returns:
        Tuple ``(gdf_line, gdf_bus)`` as in :func:`get_bus_line_geo_for_network`, with ``net``
        numbering the grids 0, 1, ... in the order of ``get_list_from_plz``.
    """
    line_frames, bus_frames = [], []
    with DatabaseClient() as dbc_client:
        for net_index, (kcid, bcid) in enumerate(dbc_client.get_list_from_plz(plz)):
            net = dbc_client.read_net_db(plz, kcid, bcid)
            line_geo, bus_geo = get_bus_line_geo_for_network(pandapower_net=net, net_index=net_index, plz=plz)
            line_frames.append(line_geo)
            bus_frames.append(bus_geo)

    return _concat(line_frames), _concat(bus_frames)


def save_geodata_as_csv(
    df_plz: pd.DataFrame,
    data_path_lines: str,
    data_path_bus: str
) -> None:
    """Write the lines and buses of all grids of several PLZ to two CSV files.

    Args:
        df_plz: DataFrame with a ``plz`` column.
        data_path_lines: Output path of the line CSV.
        data_path_bus: Output path of the bus CSV.
    """
    line_frames, bus_frames = [], []
    for plz in df_plz['plz']:
        print(f"Saving geodata of plz: {plz} to csv.")
        gdf_line, gdf_bus = get_bus_line_geo_for_plz(plz)
        line_frames.append(gdf_line)
        bus_frames.append(gdf_bus)

    _concat(line_frames).to_csv(data_path_lines)
    _concat(bus_frames).to_csv(data_path_bus)
