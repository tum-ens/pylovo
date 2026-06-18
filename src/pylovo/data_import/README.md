# pylovo data imports

This folder contains small import helpers for optional input datasets that can
improve synthetic grid generation.

## DSO transformer positions from CSV

If transformer positions are available from a distribution system operator or a
regional validation dataset, they can be imported into `pylovo.transformers` and
used as brownfield transformer roots during grid generation.

Run:

```bash
uv run pylovo-import transformers-dso-csv path/to/transformers.csv --source my_region --replace-source
```

The CSV must use WGS84 coordinates (`EPSG:4326`) and contain these columns:

| Column | Required | Description |
| --- | --- | --- |
| `external_id` | yes | Stable transformer id within the source dataset. |
| `lon` | yes | Longitude in `EPSG:4326`. |
| `lat` | yes | Latitude in `EPSG:4326`. |
| `transformer_rated_power` | no | Transformer rating in kVA, if known. |
| `source` | no | Short source label. Defaults to `csv`. Can be overridden with `--source`. |

Imported rows are stored with `type='dso'` and generated ids of the form
`dso/<source>/<external_id>`. If `--source` is provided, it overrides the optional CSV `source` column for all rows. If `--replace-source` is provided, existing rows
with the same `dso/<source>/...` prefix are deleted before importing the new
file. This is useful when re-importing corrected regional data.

To use imported DSO transformer positions in generation, set:

```yaml
USE_DSO_TRANSFORMER_POSITIONS: True
USE_OPEN_TRANSFORMER_POSITIONS: False  # or True if open/manual positions should also be used
```
