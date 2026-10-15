"""Queries of the external MV inputs: OSM power features, eGon-data tables and the MaStR units.

Reads HV/MV substation candidates and their tags from the OSM schema of InfDB, the eGon HV/MV
substations and MV grid districts from the opendata schema, and the generating units of
``pylovo.mastr_units`` and ``pylovo.mastr_postcode_aggregates``. Read-only; the methods never commit.
"""

from pylovo.database.base_mixin import BaseMixin


class MvSourceMixin(BaseMixin):
    """Read OSM, eGon and MaStR inputs of the MV stage."""
