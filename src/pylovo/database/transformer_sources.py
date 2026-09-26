"""Which rows of ``pylovo.transformers`` a generation run may use, as SQL predicates.

``pylovo.transformers`` holds candidates from three kinds of sources, told apart by ``type`` and
the ``osm_id`` prefix:

* DSO stations imported with ``pylovo-import transformers-dso-csv`` (``dso/…``, ``dso_validation/…``);
* positions placed by hand in the browser UI (``manual/…``);
* all other open data: OSM stations and LoD2 station buildings.

``USE_DSO_TRANSFORMER_POSITIONS`` enables the first, ``USE_OPEN_TRANSFORMER_POSITIONS`` all
non-DSO candidates (OSM, LoD2 and manual), and ``USE_MANUAL_TRANSFORMER_POSITIONS`` the manual
positions alone, so that a few hand-placed stations can be used without the open data.

The predicates expect the table alias ``t`` and the named parameters ``include_dso``,
``include_open`` and ``include_manual``; :func:`source_params` builds them. The doubled ``%%``
survives psycopg2 parameter substitution as a single LIKE wildcard.
"""
from __future__ import annotations

# NULL-safe: manual rows usually have ``type`` NULL, and ``NULL IN (...)`` would make the whole
# predicate (and ``NOT`` of it) NULL, which silently dropped such rows from every source.
IS_DSO_TRANSFORMER_SQL = (
    "(COALESCE(t.type, '') IN ('dso', 'dso_validation') OR COALESCE(t.osm_id, '') LIKE 'dso/%%'"
    " OR COALESCE(t.osm_id, '') LIKE 'dso_validation/%%')"
)
IS_MANUAL_TRANSFORMER_SQL = "(COALESCE(t.osm_id, '') LIKE 'manual/%%')"

SOURCE_ENABLED_SQL = (
    f"((%(include_dso)s AND {IS_DSO_TRANSFORMER_SQL})"
    f" OR (%(include_open)s AND NOT {IS_DSO_TRANSFORMER_SQL})"
    f" OR (%(include_manual)s AND {IS_MANUAL_TRANSFORMER_SQL} AND NOT {IS_DSO_TRANSFORMER_SQL}))"
)


def source_params(include_dso: bool, include_open: bool, include_manual: bool) -> dict[str, bool]:
    """The named parameters of :data:`SOURCE_ENABLED_SQL`."""
    return {"include_dso": bool(include_dso), "include_open": bool(include_open),
            "include_manual": bool(include_manual)}


def any_source(include_dso: bool, include_open: bool, include_manual: bool) -> bool:
    """True if at least one source of existing transformer positions is enabled."""
    return bool(include_dso or include_open or include_manual)
