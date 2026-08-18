"""Import DSO transformer positions from a simple CSV file."""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

import pylovo.database.database_client as dbc
from pylovo.config_loader import TARGET_EPSG

REQUIRED_COLUMNS = {"external_id", "lon", "lat"}
OPTIONAL_COLUMNS = {"transformer_rated_power", "source"}
DEFAULT_SOURCE = "csv"
DSO_TYPE = "dso"


def _normalize_source(source: str) -> str:
    """Return a compact source token that is safe to use in a generated id."""
    source = str(source or DEFAULT_SOURCE).strip().lower()
    source = re.sub(r"[^a-z0-9_.-]+", "_", source)
    return source or DEFAULT_SOURCE


def _read_transformer_csv(csv_path: Path, source: str | None = None) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        missing_text = ", ".join(sorted(missing))
        raise ValueError(f"Missing required column(s): {missing_text}")

    keep_columns = ["external_id", "lon", "lat"]
    for column in OPTIONAL_COLUMNS:
        if column in df.columns:
            keep_columns.append(column)
    df = df[keep_columns].copy()

    df["external_id"] = df["external_id"].astype(str).str.strip()
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    if "transformer_rated_power" in df.columns:
        df["transformer_rated_power"] = pd.to_numeric(df["transformer_rated_power"], errors="coerce")
    else:
        df["transformer_rated_power"] = pd.NA
    if source is not None:
        df["source"] = source
    elif "source" not in df.columns:
        df["source"] = DEFAULT_SOURCE

    invalid = df[df["external_id"].eq("") | df["lon"].isna() | df["lat"].isna()]
    if not invalid.empty:
        raise ValueError(
            "CSV contains rows with empty external_id or invalid lon/lat values. "
            f"Invalid row numbers: {list(invalid.index + 2)}"
        )

    df["source"] = df["source"].map(_normalize_source)
    df["osm_id"] = "dso/" + df["source"] + "/" + df["external_id"].astype(str)
    return df


def import_dso_transformers_csv(csv_path: str | Path, source: str | None = None, replace_source: bool = False) -> int:
    """Import DSO transformer positions into ``pylovo.transformers``.

    The CSV must contain ``external_id``, ``lon`` and ``lat`` in EPSG:4326.
    Optional columns are ``transformer_rated_power`` in kVA and ``source``.
    Passing ``source`` overrides the optional CSV source column. Imported rows
    use ``type='dso'`` and generated ids of the form
    ``dso/<source>/<external_id>``.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)

    source_token = _normalize_source(source) if source is not None else None
    df = _read_transformer_csv(csv_path, source=source_token)
    if replace_source and source_token is None:
        unique_sources = sorted(df["source"].unique())
        if len(unique_sources) != 1:
            raise ValueError("--replace-source requires --source when the CSV contains multiple sources")
        source_token = unique_sources[0]

    rows = [
        {
            "osm_id": row.osm_id,
            "transformer_rated_power": None if pd.isna(row.transformer_rated_power) else int(row.transformer_rated_power),
            "lon": float(row.lon),
            "lat": float(row.lat),
        }
        for row in df.itertuples(index=False)
    ]

    client = dbc.DatabaseClient()
    try:
        with client.conn.cursor() as cur:
            if replace_source:
                cur.execute(
                    "DELETE FROM pylovo.transformers WHERE osm_id LIKE %(prefix)s AND type = %(type)s;",
                    {"prefix": f"dso/{source_token}/%", "type": DSO_TYPE},
                )

            cur.executemany(
                f"""
                INSERT INTO pylovo.transformers (
                    osm_id,
                    area,
                    type,
                    transformer_rated_power,
                    geom_type,
                    within_shopping,
                    geom
                )
                VALUES (
                    %(osm_id)s,
                    NULL,
                    %(type)s,
                    %(transformer_rated_power)s,
                    'Point',
                    FALSE,
                    ST_Multi(ST_Transform(ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326), {TARGET_EPSG}))
                )
                ON CONFLICT (osm_id) DO UPDATE SET
                    type = EXCLUDED.type,
                    transformer_rated_power = EXCLUDED.transformer_rated_power,
                    geom_type = EXCLUDED.geom_type,
                    within_shopping = EXCLUDED.within_shopping,
                    geom = EXCLUDED.geom;
                """,
                [{**row, "type": DSO_TYPE} for row in rows],
            )
        client.conn.commit()
    except Exception:
        client.conn.rollback()
        raise
    finally:
        client.close()

    return len(rows)
