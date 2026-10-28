"""Example grids for the tutorials: read from the pylovo database, or from the example data in ``data/examples``.

The tutorial notebooks show three typical postcode areas of a pylovo run for all of Bavaria (LV version ``1``,
MV variant ``1``), one per settlement type (:data:`EXAMPLE_AREAS`). :class:`TutorialData` reads an area from
the database of the ``.env``. Without a usable database (no ``.env``, the server not reachable, or the area not
generated there) it reads the files of :func:`example_dir`, which :func:`export_example_data` writes, so the
tutorials also run right after installing pylovo.

The database is opened read-only (:meth:`~pylovo.database.database_client.DatabaseClient.read_only`). This module
imports the database client only when it is used, so it works without any pylovo configuration.

The example data derive from open data. Maps of them name their sources (:data:`EXAMPLE_ATTRIBUTION`); the
licences are listed in ``data/examples/README.md``.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from functools import cached_property
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import pandas as pd

__all__ = ["EXAMPLE_AREAS", "EXAMPLE_ATTRIBUTION", "TutorialData", "example_dir", "export_example_data"]

#: The example postcode areas: typical areas of their settlement type in Bavaria, and one typical grid of each.
EXAMPLE_AREAS: dict[int, dict[str, Any]] = {
    80805: {"name": "München (Schwabing-Freimann)", "settlement": "urban", "grid": (2, 13)},
    85256: {"name": "Vierkirchen", "settlement": "semi-urban", "grid": (2, 1)},
    85307: {"name": "Paunzhausen", "settlement": "rural", "grid": (1, 6)},
}

#: Short credit line of the example data (buildings, streets, postcode areas).
EXAMPLE_ATTRIBUTION = ("Data: © Bayerische Vermessungsverwaltung (LoD2), © basemap.de / BKG, "
                       "© OpenStreetMap contributors")

#: The layers of an example area that are stored in its GeoPackage.
_GEO_LAYERS = ("postcode", "grids", "buildings", "lines", "streets", "connections", "split_points", "mv_customers")
#: MV columns kept in the example data (the MV layers hold JSON columns that a GeoPackage cannot store).
_MV_NODE_COLUMNS = ["mv_node_id", "mv_grid_id", "kind", "grid_result_id", "plz", "sn_kva", "feeder_id", "ring_id",
                    "ring_position", "vm_load_pu", "mv_lv_tap_pos", "lv_busbar_vm_pu", "geom"]
_MV_LINE_COLUMNS = ["mv_line_id", "mv_grid_id", "from_node", "to_node", "role", "kind", "feeder_id", "ring_id",
                    "std_type", "parallel", "length_km", "normally_open", "loading_load_percent", "geom"]
_MV_GRID_COLUMNS = ["mv_grid_id", "substation_id", "vn_kv", "n_stations", "n_rings", "circuit_km", "peak_load_mw",
                    "geom"]
_MV_SUBSTATION_COLUMNS = ["substation_id", "name", "egon_bus_id", "n_transformers", "transformer_mva", "geom"]
#: Columns of the key figures of all grids in the example data.
_SUMMARY_COLUMNS = ["plz", "kcid", "bcid", "settlement_type", "transformer_rated_power", "power_flow_status",
                    "buildings", "households", "feeder_km", "service_km"]


def _zip_path(folder: Path, name: str) -> str:
    """GDAL path of the GeoPackage in ``<folder>/<name>`` (``x.gpkg.zip`` holds ``x.gpkg``); the files are zipped
    to stay below the size limit of the repository's pre-commit hook (1000 kB)."""
    return f"/vsizip/{folder / name}/{Path(name).name.removesuffix('.zip')}"


def _write_zipped_gpkg(layers: Mapping[str, Any], path: Path) -> list[str]:
    """Write the non-empty ``layers`` into a GeoPackage, zip it as ``path`` (``.gpkg.zip``) and return their names."""
    stored = []
    with tempfile.TemporaryDirectory() as tmp:
        gpkg = Path(tmp) / path.name.removesuffix(".zip")
        for name, frame in layers.items():
            if len(frame):
                frame.to_file(gpkg, layer=name, driver="GPKG")
                stored.append(name)
        path.unlink(missing_ok=True)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            archive.write(gpkg, arcname=gpkg.name)
    return stored


def example_dir() -> Path:
    """The folder with the example data.

    ``$PYLOVO_EXAMPLE_DIR`` if set, else the first ``data/examples`` with a ``manifest.json`` in the working
    directory or one of its parents (a notebook runs in ``notebook_tutorials/``), else the one of the source
    checkout this module belongs to.
    """
    explicit = os.getenv("PYLOVO_EXAMPLE_DIR")
    if explicit:
        return Path(explicit)
    here = Path.cwd().resolve()
    for folder in (here, *here.parents):
        candidate = folder / "data" / "examples"
        if (candidate / "manifest.json").is_file():
            return candidate
    return Path(__file__).resolve().parents[2] / "data" / "examples"


def _manifest(folder: Path) -> dict:
    path = folder / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _open_database(plz: int, version_id: Optional[str]) -> tuple[Any, str]:
    """A read-only client and ``""``, or None and the reason why the database cannot be used for ``plz``."""
    try:
        with contextlib.redirect_stdout(io.StringIO()):     # the configuration prints a banner if .env is missing
            from pylovo.database.database_client import DatabaseClient
    except (ValueError, ImportError) as err:
        return None, f"no database configured ({err})"
    try:
        dbc = DatabaseClient.read_only(log_file=str(Path(tempfile.gettempdir()) / "pylovo-tutorials.log"))
    except Exception as err:   # noqa: BLE001 - any connection error means: use the example data
        return None, f"the database is not reachable ({type(err).__name__})"
    from pylovo.config_loader import VERSION_ID

    dbc.cur.execute("SELECT count(*) FROM pylovo.grid_result WHERE version_id = %s AND plz = %s",
                    (VERSION_ID if version_id is None else str(version_id), int(plz)))
    if dbc.cur.fetchone()[0] == 0:
        dbc.close()
        return None, f"postcode area {plz} has no grids of version {version_id or VERSION_ID} in the database"
    return dbc, ""


class TutorialData:
    """The grids of one postcode area for the tutorials, from the database or from the example data.

    Args:
        plz: Postcode area.
        version_id: LV version; default the configured ``VERSION_ID`` (the database) or the version of the example
            data.
        source: ``"auto"`` (the database if it has the area, else the example data), ``"database"`` or
            ``"examples"``.
        folder: Folder of the example data; default :func:`example_dir`.

    Raises:
        FileNotFoundError: Neither the database nor the example data hold the area.

    Example::

        data = TutorialData(85307)
        print(data)                      # where the data come from
        net = data.net(*data.typical_grid)
    """

    def __init__(self, plz: int, version_id: Optional[str] = None, source: str = "auto",
                 folder: Optional[Path] = None):
        if source not in ("auto", "database", "examples"):
            raise ValueError("source must be 'auto', 'database' or 'examples'")
        self.plz = int(plz)
        self.folder = example_dir() if folder is None else Path(folder)
        self._dbc = None
        reason = ""
        if source in ("auto", "database"):
            self._dbc, reason = _open_database(self.plz, version_id)
            if self._dbc is None and source == "database":
                raise FileNotFoundError(f"cannot read postcode area {self.plz} from the database: {reason}")
        if self._dbc is not None:
            from pylovo.config_loader import VERSION_ID

            self.source = "database"
            self.version_id = VERSION_ID if version_id is None else str(version_id)
            self.reason = ""
            return
        manifest = _manifest(self.folder)
        area = manifest.get("areas", {}).get(str(self.plz))
        if area is None:
            raise FileNotFoundError(
                f"postcode area {self.plz} is not in the example data ({self.folder}); "
                + (reason + "; " if reason else "")
                + f"the example areas are {', '.join(manifest.get('areas', {})) or 'missing'}")
        self.source = "example data"
        self.version_id = str(manifest["version_id"])
        self.reason = reason
        self._area = area
        self._manifest = manifest

    def __repr__(self) -> str:
        where = "the database" if self.source == "database" else f"the example data in {self.folder}"
        why = f" ({self.reason})" if self.reason else ""
        return f"TutorialData(plz={self.plz}, version {self.version_id}, from {where}{why})"

    def close(self) -> None:
        """Close the database connection (if any)."""
        if self._dbc is not None:
            self._dbc.close()
            self._dbc = None

    def __del__(self):
        try:      # no module globals here: they may be gone when the interpreter shuts down
            self.close()
        except Exception:   # noqa: BLE001
            pass

    # ------------------------------------------------------------------ data
    @cached_property
    def layers(self) -> dict[str, Any]:
        """The map layers of the area (``DatabaseClient.read_lv_layers``)."""
        if self._dbc is not None:
            return self._dbc.read_lv_layers(self.plz, version_id=self.version_id)
        import geopandas as gpd

        path = _zip_path(self.folder, self._area["file"])
        result: dict[str, Any] = {}
        for name in _GEO_LAYERS:
            if name in self._area.get("layers", _GEO_LAYERS):
                result[name] = gpd.read_file(path, layer=name)
            else:
                result[name] = gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs=self._manifest["crs"])
        exclusions = self.folder / self._area["exclusions"]
        result["exclusions"] = pd.read_csv(exclusions) if exclusions.is_file() else pd.DataFrame(
            columns=["objectid", "reason"])
        return result

    @property
    def grids(self):
        """One row per grid at its MV/LV station (the ``grids`` layer)."""
        return self.layers["grids"]

    @property
    def grid_ids(self) -> list[tuple[int, int]]:
        """``(kcid, bcid)`` of all grids of the area."""
        return [(int(k), int(b)) for k, b in zip(self.grids["kcid"], self.grids["bcid"])]

    @property
    def typical_grid(self) -> tuple[int, int]:
        """The typical grid of an example area (:data:`EXAMPLE_AREAS`), else the grid with the median number of
        buildings."""
        if self.plz in EXAMPLE_AREAS:
            return tuple(EXAMPLE_AREAS[self.plz]["grid"])
        grids = self.grids.sort_values("buildings")
        row = grids.iloc[len(grids) // 2]
        return int(row["kcid"]), int(row["bcid"])

    @cached_property
    def parameters(self) -> dict:
        """The generation parameters of the version (``pylovo.version.generation_parameters``)."""
        if self._dbc is not None:
            return self._dbc.read_generation_parameters(self.version_id)
        return dict(self._manifest.get("generation_parameters") or {})

    @property
    def voltage_band(self) -> dict:
        """The voltage band of the version: LV busbar design voltage, lower and upper limit (p.u.)."""
        from pylovo.plotting.lv_grid.electrical import voltage_band

        return voltage_band(self.parameters)

    @property
    def feeder_limit_percent(self) -> float:
        """The end-to-end voltage-drop limit of the feeders of the version (% of the nominal voltage)."""
        return float(self.parameters["cable_dimensioning"]["max_end_to_end_feeder_voltage_drop_percent"])

    def net(self, kcid: int, bcid: int):
        """The pandapower net of a grid, with the results of its validation power flow.

        Raises:
            FileNotFoundError: The example data hold no net of this grid (they hold the typical grid of each area).
        """
        if self._dbc is not None:
            return self._dbc.read_net_db(self.plz, int(kcid), int(bcid), version_id=self.version_id)
        import pandapower as pp

        name = self._area.get("nets", {}).get(f"{int(kcid)}_{int(bcid)}")
        if name is None:
            raise FileNotFoundError(f"the example data hold the nets of the grids "
                                    f"{', '.join(self._area.get('nets', {}))} of postcode area {self.plz} only")
        return pp.from_json(str(self.folder / name))

    def summary(self) -> pd.DataFrame:
        """Key figures of every grid of the area (``DatabaseClient.read_grid_summary``)."""
        if self._dbc is not None:
            return self._dbc.read_grid_summary(self.plz, version_id=self.version_id)
        table = self.region_summary()
        return table[table["plz"] == self.plz].reset_index(drop=True)

    def region_summary(self, plz: Optional[Iterable[int]] = None) -> pd.DataFrame:
        """Key figures of every grid of many postcode areas: all of the version (database), or of the example
        data (all postcode areas of the Bavaria-wide run). ``plz`` limits the areas."""
        if self._dbc is not None:
            return self._dbc.read_grid_summary(plz, version_id=self.version_id)
        table = pd.read_csv(self.folder / self._manifest["grid_summary"])
        return table if plz is None else table[table["plz"].isin([int(p) for p in plz])].reset_index(drop=True)

    def postcodes(self) -> pd.DataFrame:
        """Settlement type and density metrics of all postcode areas of the version."""
        if self._dbc is not None:
            return self._dbc.read_postcode_metrics(self.version_id)
        return pd.read_csv(self.folder / self._manifest["postcodes"])

    def voltage_levels(self, kcid: Optional[int] = None, bcid: Optional[int] = None) -> dict[str, Any]:
        """Everything :func:`pylovo.plotting.voltage_levels.plot_voltage_levels` needs for one grid.

        Returns:
            ``{"chain", "hv", "mv_context", "mv_grid", "lv"}``; ``chain`` is None if the version has no MV
            results for the grid. The HV grid is read from the download of ``pylovo-viewer fetch etrago``
            (database) or from the example data.
        """
        kcid, bcid = (kcid, bcid) if kcid is not None else self.typical_grid
        if self._dbc is not None:
            from pylovo.plotting.voltage_levels import read_hv_grid

            chain = self._dbc.read_voltage_chain(self.plz, kcid, bcid, version_id=self.version_id)
            if chain is None:
                return {"chain": None}
            variant = chain["mv_variant_id"]
            context = self._dbc.read_mv_layers(self.version_id, variant, layers=["substations", "grids"])
            mv_grid = self._dbc.read_mv_layers(self.version_id, variant, chain["mv_grid_id"],
                                               layers=["nodes", "lines", "substations", "grids"])
            try:
                hv = read_hv_grid()
            except FileNotFoundError:
                hv = {}
            return {"chain": chain, "hv": hv, "mv_context": context, "mv_grid": mv_grid, "lv": self.layers}
        import geopandas as gpd

        entry = self._area.get("voltage_levels", {}).get(f"{int(kcid)}_{int(bcid)}")
        if entry is None:
            return {"chain": None}
        path = _zip_path(self.folder, entry["file"])
        read = lambda layer: gpd.read_file(path, layer=layer)    # noqa: E731
        return {"chain": entry["chain"], "hv": {"lines": read("hv_lines"), "buses": read("hv_buses")},
                "mv_context": {"substations": read("context_substations"), "grids": read("context_grids")},
                "mv_grid": {"nodes": read("mv_nodes"), "lines": read("mv_lines"),
                            "substations": read("mv_substation"), "grids": read("mv_grid")},
                "lv": self.layers}


# --------------------------------------------------------------------------- export (maintainers)
def _geo_frame(frame, columns: Optional[list[str]] = None):
    """A copy that a GeoPackage can store: dict and list values as JSON text, the geometry column ``geometry``."""
    frame = frame.copy() if columns is None else frame[[c for c in columns if c in frame.columns]].copy()
    geometry = frame.geometry.name
    for column in frame.columns:
        if column != geometry and frame[column].dtype == object:
            frame[column] = frame[column].map(lambda v: json.dumps(v) if isinstance(v, (dict, list)) else v)
    return frame.rename_geometry("geometry") if geometry != "geometry" else frame


def export_example_data(folder: Path, areas: Mapping[int, Mapping[str, Any]] = EXAMPLE_AREAS,
                        version_id: str = "1", hv_dir: Optional[Path] = None, region_km: float = 40.0) -> dict:
    """Write the example data of the tutorials from the database (read-only); for maintainers.

    Per area a zipped GeoPackage with the layers of ``read_lv_layers``, the building exclusions as CSV, the pandapower
    net of the typical grid as JSON and the context of the voltage-level figure (HV grid, MV supply areas, the MV
    grid of the typical grid); for the version the postcode metrics and the key figures of every grid (CSV), and
    ``manifest.json``.

    Args:
        folder: Output folder, e.g. ``data/examples``.
        areas: ``{plz: {"name", "settlement", "grid": (kcid, bcid)}}``.
        version_id: LV version.
        hv_dir: Folder of the eTraGo download (``pylovo-viewer fetch etrago``).
        region_km: Width of the region kept around an MV supply area for the voltage-level figure.

    Returns:
        The manifest.
    """
    import geopandas as gpd
    import pandapower as pp

    from pylovo import __version__
    from pylovo.config_loader import TARGET_EPSG
    from pylovo.database.database_client import DatabaseClient
    from pylovo.plotting.voltage_levels import read_hv_grid

    folder = Path(folder)
    (folder / "nets").mkdir(parents=True, exist_ok=True)
    (folder / "voltage_levels").mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "description": "Example grids of the pylovo tutorials (TutorialData without a database)",
        "version_id": str(version_id), "crs": f"EPSG:{TARGET_EPSG}",
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "pylovo": __version__,
        "attribution": EXAMPLE_ATTRIBUTION, "postcodes": "postcodes.csv", "grid_summary": "grid_summary.csv.xz",
        "areas": {},
    }
    hv = read_hv_grid(hv_dir)
    with DatabaseClient.read_only(log_file=str(Path(tempfile.gettempdir()) / "pylovo-tutorials.log")) as dbc:
        manifest["generation_parameters"] = dbc.read_generation_parameters(version_id)
        dbc.read_postcode_metrics(version_id).to_csv(folder / "postcodes.csv", index=False, float_format="%.4g")
        # three significant digits and xz keep the table of all grids of the version at about 1 MB
        summary = dbc.read_grid_summary(None, version_id=version_id)
        summary[[c for c in _SUMMARY_COLUMNS if c in summary]].to_csv(
            folder / "grid_summary.csv.xz", index=False, float_format="%.3g", compression="xz")
        for plz, area in areas.items():
            layers = dbc.read_lv_layers(plz, version_id=version_id)
            path = folder / f"{plz}.gpkg.zip"
            stored = _write_zipped_gpkg({name: _geo_frame(layers[name]) for name in _GEO_LAYERS}, path)
            layers["exclusions"].to_csv(folder / f"{plz}_exclusions.csv", index=False)
            kcid, bcid = (int(v) for v in area["grid"])
            net_name = f"nets/{plz}_{kcid}_{bcid}.json"
            pp.to_json(dbc.read_net_db(plz, kcid, bcid, version_id=version_id), str(folder / net_name))
            entry = {"name": area["name"], "settlement": area["settlement"], "grid": [kcid, bcid],
                     "file": path.name, "layers": stored, "exclusions": f"{plz}_exclusions.csv",
                     "nets": {f"{kcid}_{bcid}": net_name}, "voltage_levels": {}}
            chain = dbc.read_voltage_chain(plz, kcid, bcid, version_id=version_id)
            if chain is not None:
                variant = chain["mv_variant_id"]
                mv_grid = dbc.read_mv_layers(version_id, variant, chain["mv_grid_id"],
                                             layers=["nodes", "lines", "substations", "grids"])
                context = dbc.read_mv_layers(version_id, variant, layers=["substations", "grids"])
                minx, miny, maxx, maxy = mv_grid["grids"].total_bounds
                half = max(maxx - minx, maxy - miny, region_km * 1000) * 0.9
                cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
                box = (cx - half, cy - half, cx + half, cy + half)
                vl_path = folder / "voltage_levels" / f"{plz}_{kcid}_{bcid}.gpkg.zip"
                parts = {
                    "hv_lines": hv["lines"].cx[box[0]:box[2], box[1]:box[3]],
                    "hv_buses": hv["buses"].cx[box[0]:box[2], box[1]:box[3]],
                    "context_substations": _geo_frame(context["substations"], _MV_SUBSTATION_COLUMNS)
                    .cx[box[0]:box[2], box[1]:box[3]],
                    "context_grids": _geo_frame(context["grids"], _MV_GRID_COLUMNS).cx[box[0]:box[2], box[1]:box[3]],
                    "mv_nodes": _geo_frame(mv_grid["nodes"], _MV_NODE_COLUMNS),
                    "mv_lines": _geo_frame(mv_grid["lines"], _MV_LINE_COLUMNS),
                    "mv_substation": _geo_frame(mv_grid["substations"], _MV_SUBSTATION_COLUMNS),
                    "mv_grid": _geo_frame(mv_grid["grids"], _MV_GRID_COLUMNS),
                }
                _write_zipped_gpkg({name: gpd.GeoDataFrame(frame) for name, frame in parts.items()}, vl_path)
                entry["voltage_levels"][f"{kcid}_{bcid}"] = {"file": f"voltage_levels/{vl_path.name}",
                                                            "chain": chain}
            manifest["areas"][str(plz)] = entry
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=1, default=str) + "\n", encoding="utf-8")
    return manifest
