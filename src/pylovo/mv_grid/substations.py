"""HV/MV substations of the MV stage: OSM filter rules, aggregation into sites and the eGon cross-check.

The candidates are the ``power=substation`` features of the OSM schema of InfDB within the search buffer of the
planning region (:meth:`MvSourceMixin.osm_substation_candidates
<pylovo.database.mv_source_mixin.MvSourceMixin.osm_substation_candidates>`, one row per feature). This module
decides which of them are HV/MV substations and merges them into sites, after the rules of Hülk et al. (2017),
adapted:

1. :func:`classify_candidate` keeps a candidate only if its ``voltage`` tag holds 110 kV or 60 kV, and drops
   traction, transition, converter, generation, industrial and minor-distribution substations, railway
   frequencies (16.7 Hz) and DC, railway operators and names, and substations inside a power plant or generator
   polygon. A ``transmission`` tag with 110 kV is kept (many DSO substations are tagged so).
2. :func:`aggregate` merges the kept candidates whose footprints or points lie within ``substation_merge_m`` of each
   other into one :class:`Site`, drops extra-high-voltage sites without an MV level next to an HV/MV site
   (``ehv_partner_m``; alone they are kept with the flag ``ehv_uncertain``) and, of two sites closer than
   ``substation_drop_m``, keeps the one with the larger footprint.
3. :func:`match_egon` gives each site the nearest eGon-data HV/MV substation within ``egon_match_m``;
   :func:`egon_sites` turns the eGon substations into sites for ``MV_SUBSTATIONS: egon``.

:func:`select_osm_sites` runs steps 1 and 2 with the parameters of an MV variant; :func:`sites_frame` gives the
sites as a table (columns of ``pylovo.mv_substation``). Optional transformer ratings of the OSM footprints are
parsed by :func:`site_ratings` and :func:`known_units_mva`.

Pure: no database, configuration or plotting import. Coordinates are in ``TARGET_EPSG`` (metres).
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping, Sequence

import numpy as np
import pandas as pd
import shapely
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from shapely.errors import GEOSException
from shapely.strtree import STRtree

from pylovo.mv_grid.parameters import MvStageError

if TYPE_CHECKING:   # pragma: no cover
    from pylovo.mv_grid.parameters import MvParameters

#: Voltages (V) of the HV level of an HV/MV substation; a candidate needs one of them.
HV_LEVELS_V: frozenset[int] = frozenset({60000, 110000})
#: Lowest voltage (V) of the extra-high-voltage level (220 kV and 380 kV).
EHV_MIN_V: int = 220000
#: Voltage range (V, inclusive) of the MV level (1 kV to 36 kV).
MV_RANGE_V: tuple[int, int] = (1000, 36000)

#: ``substation=*`` values that are no public HV/MV substation (rule 2).
DROPPED_SUBSTATION_TAGS: frozenset[str] = frozenset(
    {"traction", "transition", "converter", "generation", "industrial", "minor_distribution"})
_RAILWAY_OPERATOR = re.compile(r"db energie|deutsche bahn|db netz|bahnstrom", re.IGNORECASE)
_RAILWAY_NAME = re.compile(r"unterwerk|bahnstrom|umformerwerk|umrichter", re.IGNORECASE)
_RAILWAY_FREQUENCY = re.compile(r"16\s*[.,]\s*6*7|16\s+2\s*/\s*3")

#: Columns of the candidate table (:meth:`MvSourceMixin.osm_substation_candidates`).
CANDIDATE_COLUMNS: tuple[str, ...] = ("source_id", "voltage", "substation_tag", "frequency", "operator", "name",
                                      "railway", "x", "y", "area_m2", "footprint_wkb", "in_plant", "plant")
#: ``plant:source`` values of thermal plants and names of heating plants (``plant`` column, ``source:name``): a
#: substation inside such a plant is kept, because city utilities run their HV/MV substations on their combined heat
#: and power and heating plant sites.
_THERMAL_PLANT = re.compile(r"(^|\|)(gas|coal|oil|biomass|biogas|waste|lignite|diesel)(;[a-z_;]*)?:|heiz(kraft)?werk|"
                            r"\bhkw\b|\bbhkw\b|kraft-?wärme|\bkwk\b|\bchp\b")
#: Reasons of :func:`classify_candidate` (``kept`` for a kept candidate).
CANDIDATE_REASONS: tuple[str, ...] = ("kept", "no_hv", *(f"tag_{t}" for t in sorted(DROPPED_SUBSTATION_TAGS)),
                                      "frequency", "railway_operator", "railway_name", "railway_tag", "in_plant")
#: Reasons of the site rules of :func:`aggregate`.
SITE_DROP_REASONS: tuple[str, ...] = ("ehv_partner", "near_larger")
#: Columns of :func:`sites_frame`.
SITE_COLUMNS: tuple[str, ...] = ("source", "source_ids", "name", "operator", "voltage_tag", "max_v", "mv_tagged",
                                 "egon_bus_id", "egon_distance_m", "in_region", "osm_ratings_mva", "flags", "x", "y",
                                 "area_m2", "footprint_wkb")

_NUMBER = r"(\d+(?:\.\d+)?)"
_VOLTAGE_PART = re.compile(rf"^{_NUMBER}\s*(kv|v)?$")
_RATING_PART = re.compile(rf"^{_NUMBER}\s*(mva|kva|va|mw|kw)?$")
_SEPARATORS = re.compile(r"[;/|]")
_DECIMAL_COMMA = re.compile(r"(?<=\d),(?=\d{1,2}(?!\d))")


# ---------------------------------------------------------------------------------------------- tag parsing
def _missing(value: Any) -> bool:
    """True for None, NaN and pd.NA."""
    return value is None or value is pd.NA or (isinstance(value, float) and math.isnan(value))


def _text(value: Any) -> str | None:
    """A stripped text, or None for None, NaN and empty values."""
    if _missing(value):
        return None
    text = str(value).strip()
    return text or None


def _number(value: Any) -> float:
    """A float, or 0.0 for None, NaN and values that are no number."""
    if _missing(value):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _flag(value: Any) -> bool:
    """True for a true boolean-like value; None and NaN are False."""
    if _missing(value):
        return False
    if isinstance(value, str):
        return value.strip().lower() in ("t", "true", "1", "yes")
    return bool(value)


def _number_parts(text: str | None, pattern: re.Pattern) -> list[tuple[float, str | None]]:
    """``(number, unit)`` of each part of a multi-valued OSM tag; parts that are no number are skipped."""
    text = _text(text)
    if text is None:
        return []
    parts: list[tuple[float, str | None]] = []
    for chunk in _SEPARATORS.split(text.lower().replace("_", " ")):
        chunk = _DECIMAL_COMMA.sub(".", chunk)   # '10,5 kV' is 10.5 kV, '110000,20000' two values
        for part in chunk.split(","):
            match = pattern.match(part.strip())
            if match:
                parts.append((float(match.group(1)), match.group(2)))
    return parts


def parse_voltages(text: str | None) -> set[int]:
    """The voltages (V) of an OSM ``voltage`` tag.

    The tag is split at ``;`` (also ``/``, ``|`` and a comma between values); each part is a number with an
    optional unit ``V`` or ``kV`` (``110 kV``, ``110kV``, ``20_kv``). A number without unit is in volts if it is at
    least 1,000. A smaller number is read as kV (``110`` is 110 kV, ``0.4`` is 400 V), except an integer next to a
    value of at least 1,000 V in the same tag, which is an LV value in volts (``20000;400`` is 20 kV and 400 V).
    Parts that are no number (``medium``, ``unknown``) are skipped.

    Returns:
        The distinct voltages in volts (empty if the tag is missing or holds no number).
    """
    parts = _number_parts(text, _VOLTAGE_PART)
    volts_context = any(unit is None and value >= 1000 for value, unit in parts)
    voltages: set[int] = set()
    for value, unit in parts:
        if unit == "kv" or (unit is None and value < 1000 and not (volts_context and value.is_integer())):
            value *= 1000.0
        if value >= 1.0:
            voltages.add(int(round(value)))
    return voltages


def parse_rating_mva(text: str | None) -> float | None:
    """The rated power (MVA) of an OSM ``rating`` tag of a transformer.

    Accepts ``40 MVA``, ``40MVA``, ``31,5 MVA``, ``40000 kVA`` and ``VA`` (``MW``/``kW`` are read as MVA/kVA). A
    number without unit is in MVA below 1,000, in kVA below 1,000,000 and in VA above. A tag with several values
    (``31.5 MVA;40 MVA``, cooling stages) gives the largest.

    Returns:
        The rating in MVA, or None if the tag is missing or holds no positive number.
    """
    ratings = []
    for value, unit in _number_parts(text, _RATING_PART):
        if unit in ("mva", "mw"):
            mva = value
        elif unit in ("kva", "kw"):
            mva = value / 1e3
        elif unit == "va":
            mva = value / 1e6
        else:
            mva = value if value < 1e3 else (value / 1e3 if value < 1e6 else value / 1e6)
        if mva > 0:
            ratings.append(mva)
    return max(ratings) if ratings else None


# ---------------------------------------------------------------------------------------------- filter rules
def classify_candidate(row: Mapping[str, Any]) -> tuple[bool, str]:
    """Decide whether an OSM substation candidate is an HV/MV substation (Hülk et al. 2017, adapted).

    The rules are applied in this order; the first that matches gives the reason:

    1. ``no_hv``: the ``voltage`` tag holds neither 110 kV nor 60 kV. A mapped footprint (``area_m2`` > 0) without any
       voltage tagged and with ``substation=transmission`` counts as HV (city utilities tag 110 kV substations so and
       leave out the voltage); a point needs the voltage.
    2. ``tag_<value>``: ``substation`` is ``traction``, ``transition``, ``converter``, ``generation``,
       ``industrial`` or ``minor_distribution`` (``transmission`` with 110 kV is kept).
    3. ``frequency``: ``frequency`` holds 16.7 Hz (``16.7``, ``16,7``, ``16.67``, ``16 2/3``) or is ``0`` (DC).
    4. ``railway_operator``, ``railway_name``, ``railway_tag``: a railway operator (DB Energie, Deutsche Bahn,
       DB Netz, Bahnstrom), a railway substation name (Unterwerk, Bahnstrom, Umformerwerk, Umrichter) or a
       ``railway`` tag.
    5. ``in_plant``: the candidate lies inside a power plant or generator polygon (a substation of a plant or PV
       park), unless the plant is a thermal or heating plant (:data:`_THERMAL_PLANT` on ``plant``): city utilities
       run HV/MV substations on their combined heat and power plant sites.

    Args:
        row: A candidate with the keys of :data:`CANDIDATE_COLUMNS` (a dict or a pandas row); missing keys and NaN
            count as missing tags.

    Returns:
        ``(True, "kept")`` or ``(False, reason)``.
    """
    voltages = parse_voltages(row.get("voltage"))
    tag = (_text(row.get("substation_tag")) or "").lower()
    footprint = _number(row.get("area_m2")) > 0
    if not voltages & HV_LEVELS_V and (_text(row.get("voltage")) is not None or tag != "transmission" or not footprint):
        return False, "no_hv"
    if tag in DROPPED_SUBSTATION_TAGS:
        return False, f"tag_{tag}"
    frequency = _text(row.get("frequency")) or ""
    if _RAILWAY_FREQUENCY.search(frequency) or any(part.strip() in ("0", "0.0") for part in frequency.split(";")):
        return False, "frequency"
    if _RAILWAY_OPERATOR.search(_text(row.get("operator")) or ""):
        return False, "railway_operator"
    if _RAILWAY_NAME.search(_text(row.get("name")) or ""):
        return False, "railway_name"
    if (_text(row.get("railway")) or "no").lower() != "no":
        return False, "railway_tag"
    if _flag(row.get("in_plant")) and not _THERMAL_PLANT.search(_text(row.get("plant")) or ""):
        return False, "in_plant"
    return True, "kept"


def filter_candidates(rows: pd.DataFrame) -> pd.DataFrame:
    """The candidates with the decision of :func:`classify_candidate` in the columns ``keep`` and ``reason``."""
    decisions = rows.copy()
    results = [classify_candidate(row) for row in rows.to_dict("records")]
    decisions["keep"] = pd.Series([keep for keep, _ in results], index=rows.index, dtype=bool)
    decisions["reason"] = pd.Series([reason for _, reason in results], index=rows.index, dtype=object)
    return decisions


# ---------------------------------------------------------------------------------------------- sites
@dataclass
class Site:
    """One HV/MV substation site (busbar) made of one or more merged candidates, or of one eGon substation.

    Attributes:
        source_ids: Sorted ids of the merged features: ``node/<id>``, ``way/<id>``, ``relation/<id>`` or
            ``egon/<bus_id>``.
        x: Busbar point (``TARGET_EPSG``): the representative point of the largest footprint polygon, else the
            mean of the candidate points.
        y: See ``x``.
        area_m2: Area of the footprint (0 without one).
        footprint_wkb: WKB of the footprint (MultiPolygon, the union of the candidate polygons), or None.
        name: Name of the largest named candidate.
        operator: Operator of the largest candidate with an operator.
        voltage_tag: The distinct raw voltage tags of the candidates, sorted and joined with ``|``.
        max_v: Highest voltage of the site (V).
        mv_tagged: An MV level (1 kV to 36 kV) is tagged.
        flags: ``ehv_uncertain`` for an extra-high-voltage site without MV tag and without an HV/MV site nearby;
            the stage adds its own flags (``long_attach``, ``over_capacity``, ``no_nodes``).
    """

    source_ids: list[str]
    x: float
    y: float
    area_m2: float
    footprint_wkb: bytes | None
    name: str | None
    operator: str | None
    voltage_tag: str
    max_v: int
    mv_tagged: bool
    flags: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """The first source id (identifies the site in logs and tie-breaks)."""
        return self.source_ids[0] if self.source_ids else ""

    @property
    def source(self) -> str:
        """``egon`` for an eGon substation, else ``osm`` (the ``source`` column of ``pylovo.mv_substation``)."""
        return "egon" if self.key.startswith("egon/") else "osm"

    def point(self) -> shapely.Point:
        """The busbar point."""
        return shapely.Point(self.x, self.y)

    def geometry(self) -> shapely.Geometry:
        """The footprint, or the busbar point for a site without one."""
        if self.footprint_wkb is not None:
            return shapely.from_wkb(self.footprint_wkb)
        return self.point()


def _polygonal(value: Any) -> shapely.Geometry | None:
    """A valid (Multi)Polygon from WKB (bytes, memoryview or hex); None if missing, empty or not polygonal."""
    if _missing(value):
        return None
    try:
        geom = shapely.make_valid(shapely.from_wkb(value if isinstance(value, str) else bytes(value)))
    except (GEOSException, TypeError, ValueError):
        return None
    parts = [part for part in shapely.get_parts(geom) if part.geom_type in ("Polygon", "MultiPolygon")]
    if not parts:
        return None
    polygon = shapely.union_all(parts)
    return None if polygon.is_empty else polygon


def _multipolygon(geom: shapely.Geometry) -> shapely.MultiPolygon:
    polygons = [part for part in shapely.get_parts(geom) if part.geom_type == "Polygon"]
    return shapely.MultiPolygon(polygons)


def _merge_group(members: pd.DataFrame, footprints: list[shapely.Geometry | None]) -> Site:
    """One site from candidates that lie within the merge distance of each other (sorted by source id)."""
    polygons = [fp for fp in footprints if fp is not None]
    if polygons:
        footprint = _multipolygon(shapely.union_all(polygons))
        largest = max(footprint.geoms, key=lambda poly: poly.area)
        busbar = largest.representative_point()
        x, y, area, footprint_wkb = busbar.x, busbar.y, float(footprint.area), shapely.to_wkb(footprint)
    else:
        x, y = float(members["x"].astype(float).mean()), float(members["y"].astype(float).mean())
        area, footprint_wkb = 0.0, None
    areas = [fp.area if fp is not None else 0.0 for fp in footprints]
    ranked = [record for _, record in sorted(zip(areas, members.to_dict("records")),
                                             key=lambda item: (-item[0], str(item[1]["source_id"])))]
    name = next((_text(r.get("name")) for r in ranked if _text(r.get("name"))), None)
    operator = next((_text(r.get("operator")) for r in ranked if _text(r.get("operator"))), None)
    tags = sorted({tag for tag in (_text(r.get("voltage")) for r in ranked) if tag})
    voltages = set().union(*(parse_voltages(r.get("voltage")) for r in ranked))
    return Site(
        source_ids=sorted(str(s) for s in members["source_id"]),
        x=float(x), y=float(y), area_m2=area, footprint_wkb=footprint_wkb,
        name=name, operator=operator, voltage_tag="|".join(tags),
        max_v=max(voltages, default=0),
        mv_tagged=any(MV_RANGE_V[0] <= v <= MV_RANGE_V[1] for v in voltages),
        flags=[],
    )


def aggregate(rows: pd.DataFrame, merge_m: float, drop_m: float, ehv_partner_m: float, *,
              dropped: list[tuple[Site, str, str]] | None = None) -> list[Site]:
    """Merge the kept candidates into sites and apply the EHV rule and the duplicate rule.

    1. **Merge:** candidates whose footprints (or points) lie within ``merge_m`` of each other are one site
       (transitively); the footprint is their union and the busbar the representative point of its largest
       polygon.
    2. **EHV rule:** a site whose highest voltage is at least 220 kV and that has no MV level tagged is dropped
       (reason ``ehv_partner``) if a site with a highest voltage of at most 110 kV lies within ``ehv_partner_m``;
       otherwise it is kept with the flag ``ehv_uncertain``.
    3. **Duplicate rule:** of two sites closer than ``drop_m``, the one with the smaller footprint area is dropped
       (reason ``near_larger``; points have area 0, ties keep the smaller source id).

    Distances are measured between footprints, or points for candidates without a footprint.

    Args:
        rows: The kept candidates (columns of :data:`CANDIDATE_COLUMNS`; see :func:`filter_candidates`).
        merge_m: ``substation_merge_m``.
        drop_m: ``substation_drop_m``.
        ehv_partner_m: ``ehv_partner_m``.
        dropped: If given, receives ``(site, reason, key of the site that caused the drop)`` for every site
            removed by rule 2 or 3.

    Returns:
        The sites, sorted by their first source id.
    """
    if rows is None or len(rows) == 0:
        return []
    rows = rows.sort_values("source_id", kind="stable").reset_index(drop=True)
    footprints = [_polygonal(value) for value in rows["footprint_wkb"]] if "footprint_wkb" in rows else \
        [None] * len(rows)
    points = shapely.points(rows["x"].to_numpy(float), rows["y"].to_numpy(float))
    geoms = np.array([fp if fp is not None else pt for fp, pt in zip(footprints, points)], dtype=object)
    left, right = STRtree(geoms).query(geoms, predicate="dwithin", distance=float(merge_m))
    graph = coo_matrix((np.ones(len(left)), (left, right)), shape=(len(rows), len(rows)))
    _, label = connected_components(graph, directed=False)
    groups: dict[int, list[int]] = {}
    for index, group in enumerate(label):
        groups.setdefault(int(group), []).append(index)
    sites = [_merge_group(rows.iloc[members], [footprints[i] for i in members]) for members in groups.values()]
    sites = _ehv_rule(sites, ehv_partner_m, dropped)
    sites = _duplicate_rule(sites, drop_m, dropped)
    return sorted(sites, key=lambda site: site.key)


def _ehv_rule(sites: list[Site], partner_m: float, dropped: list | None) -> list[Site]:
    partners = [site for site in sites if 0 < site.max_v <= 110000]
    partner_geoms = np.array([site.geometry() for site in partners], dtype=object)
    tree = STRtree(partner_geoms) if len(partners) else None
    kept = []
    for site in sites:
        if site.max_v < EHV_MIN_V or site.mv_tagged:
            kept.append(site)
            continue
        near = tree.query(site.geometry(), predicate="dwithin", distance=float(partner_m)) if tree else []
        if len(near):
            distances = shapely.distance(site.geometry(), partner_geoms[near])
            partner = partners[int(near[int(np.argmin(distances))])]
            if dropped is not None:
                dropped.append((site, "ehv_partner", partner.key))
            continue
        site.flags.append("ehv_uncertain")
        kept.append(site)
    return kept


def _duplicate_rule(sites: list[Site], drop_m: float, dropped: list | None) -> list[Site]:
    order = sorted(sites, key=lambda site: (-site.area_m2, site.key))
    kept: list[Site] = []
    kept_geoms: list[shapely.Geometry] = []
    for site in order:
        geom = site.geometry()
        if kept_geoms:
            distances = shapely.distance(geom, np.array(kept_geoms, dtype=object))
            closest = int(np.argmin(distances))
            if distances[closest] < drop_m:
                if dropped is not None:
                    dropped.append((site, "near_larger", kept[closest].key))
                continue
        kept.append(site)
        kept_geoms.append(geom)
    return kept


@dataclass
class SiteSelection:
    """Result of :func:`select_osm_sites`.

    Attributes:
        sites: The HV/MV substation sites, sorted by their first source id.
        decisions: The candidates with ``keep`` and ``reason`` of :func:`classify_candidate`.
        dropped: Sites removed by the EHV and duplicate rules: ``(site, reason, key of the other site)``.
    """

    sites: list[Site]
    decisions: pd.DataFrame
    dropped: list[tuple[Site, str, str]]

    def reason_counts(self) -> dict[str, int]:
        """Number of dropped candidates per filter reason and of dropped sites per site rule."""
        counts = Counter(r for r in self.decisions.get("reason", pd.Series(dtype=object)) if r != "kept")
        counts.update(reason for _, reason, _ in self.dropped)
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    def describe(self) -> str:
        """One log line: candidates, kept, sites, merges, flags and drop reasons."""
        n_kept = int(self.decisions["keep"].sum()) if "keep" in self.decisions else 0
        merged = sum(len(site.source_ids) - 1 for site in self.sites)
        uncertain = sum("ehv_uncertain" in site.flags for site in self.sites)
        reasons = ", ".join(f"{reason} {count}" for reason, count in self.reason_counts().items()) or "none"
        return (f"OSM substations: {len(self.decisions)} candidates, {n_kept} kept, {len(self.sites)} sites "
                f"({merged} candidates merged, {uncertain} ehv_uncertain); dropped: {reasons}")


def select_osm_sites(candidates: pd.DataFrame, params: "MvParameters") -> SiteSelection:
    """The HV/MV substation sites of the OSM candidates with the parameters of an MV variant.

    Runs :func:`filter_candidates` and :func:`aggregate` with ``substation_merge_m``, ``substation_drop_m`` and
    ``ehv_partner_m``.

    Raises:
        MvStageError: If there is no candidate in the search buffer, or none passes the filter.
    """
    decisions = filter_candidates(candidates)
    dropped: list[tuple[Site, str, str]] = []
    kept = decisions[decisions["keep"]]
    sites = aggregate(kept, params.substation_merge_m, params.substation_drop_m, params.ehv_partner_m,
                      dropped=dropped)
    selection = SiteSelection(sites=sites, decisions=decisions, dropped=dropped)
    if not sites:
        km = f"{params.region_buffer_m / 1000:g} km"
        if len(candidates) == 0:
            raise MvStageError(f"no HV/MV substations within {km} of the region; check INFDB_OSM_SCHEMA or use "
                               "MV_SUBSTATIONS=egon")
        reasons = ", ".join(f"{reason} {count}" for reason, count in selection.reason_counts().items())
        raise MvStageError(f"none of the {len(candidates)} OSM substations within {km} of the region is an HV/MV "
                           f"substation ({reasons}); check INFDB_OSM_SCHEMA or use MV_SUBSTATIONS=egon")
    return selection


# ---------------------------------------------------------------------------------------------- eGon
def _egon_points(egon: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    valid = egon.dropna(subset=["x", "y", "bus_id"])
    return (shapely.points(valid["x"].to_numpy(float), valid["y"].to_numpy(float)),
            valid["bus_id"].to_numpy(np.int64))


def match_egon(sites: Sequence[Site], egon: pd.DataFrame, max_m: float) -> list[tuple[int | None, float | None]]:
    """The nearest eGon-data HV/MV substation of each site within ``max_m``.

    The distance is measured from the site's footprint (0 if the eGon point lies inside it), or from its busbar
    point; ties go to the smaller ``bus_id``. Several sites may match the same eGon substation.

    Args:
        sites: The sites.
        egon: eGon substations with ``bus_id``, ``x`` and ``y`` (``TARGET_EPSG``).
        max_m: ``egon_match_m``.

    Returns:
        ``(bus_id, distance_m)`` per site, ``(None, None)`` without a match.
    """
    if not len(sites):
        return []
    if egon is None or len(egon) == 0:
        return [(None, None)] * len(sites)
    points, bus_ids = _egon_points(egon)
    tree = STRtree(points)
    matches: list[tuple[int | None, float | None]] = []
    for site in sites:
        geom = site.geometry()
        near = tree.query(geom, predicate="dwithin", distance=float(max_m))
        if not len(near):
            matches.append((None, None))
            continue
        distances = shapely.distance(geom, points[near])
        best = np.lexsort((bus_ids[near], distances))[0]
        matches.append((int(bus_ids[near][best]), float(distances[best])))
    return matches


def unmatched_egon(sites: Sequence[Site], egon: pd.DataFrame, max_m: float) -> list[int]:
    """The ``bus_id`` of the eGon substations without a site within ``max_m`` (for the cross-check summary)."""
    if egon is None or len(egon) == 0:
        return []
    points, bus_ids = _egon_points(egon)
    if not len(sites):
        return sorted(int(b) for b in bus_ids)
    geoms = np.array([site.geometry() for site in sites], dtype=object)
    tree = STRtree(geoms)
    return sorted(int(bus) for point, bus in zip(points, bus_ids)
                  if not len(tree.query(point, predicate="dwithin", distance=float(max_m))))


def egon_sites(egon: pd.DataFrame) -> list[Site]:
    """The eGon-data HV/MV substations as sites (for ``MV_SUBSTATIONS: egon``), sorted by ``bus_id``.

    Each substation is one site ``egon/<bus_id>`` without footprint. eGon-data has filtered and aggregated them
    already, so the OSM rules are not applied. Without a readable voltage the site counts as 110 kV.

    Args:
        egon: eGon substations with ``bus_id``, ``voltage``, ``x`` and ``y`` (``TARGET_EPSG``).
    """
    if egon is None or len(egon) == 0:
        return []
    sites = []
    for row in egon.dropna(subset=["x", "y", "bus_id"]).sort_values("bus_id").to_dict("records"):
        tag = _text(row.get("voltage"))
        voltages = parse_voltages(tag)
        sites.append(Site(
            source_ids=[f"egon/{int(row['bus_id'])}"], x=float(row["x"]), y=float(row["y"]), area_m2=0.0,
            footprint_wkb=None, name=_text(row.get("name")), operator=_text(row.get("operator")),
            voltage_tag=tag or "", max_v=max(voltages, default=110000),
            mv_tagged=any(MV_RANGE_V[0] <= v <= MV_RANGE_V[1] for v in voltages), flags=[],
        ))
    return sites


# ---------------------------------------------------------------------------------------------- tables
def sites_frame(sites: Sequence[Site], region_wkb: bytes | None = None,
                matches: Sequence[tuple[int | None, float | None]] | None = None,
                ratings: Sequence[Sequence[float]] | None = None) -> pd.DataFrame:
    """The sites as a table with the columns of :data:`SITE_COLUMNS` (one row per site, in the given order).

    Args:
        sites: The sites.
        region_wkb: The planning region (WKB, ``TARGET_EPSG``); ``in_region`` is True if the busbar point lies in
            it. Without a region ``in_region`` is None.
        matches: The result of :func:`match_egon` (``egon_bus_id``, ``egon_distance_m``); None leaves them empty.
            With matches, a site without one gets the flag ``no_egon_bus``: it has no bus in the eGon-data grid
            model, whose HV/MV bus ids are the 110 kV buses of eTraGo.
        ratings: OSM transformer ratings per site (:func:`site_ratings`); None or an empty entry gives None.
    """
    n = len(sites)
    if matches is not None and len(matches) != n:
        raise ValueError(f"matches has {len(matches)} entries for {n} sites")
    if ratings is not None and len(ratings) != n:
        raise ValueError(f"ratings has {len(ratings)} entries for {n} sites")
    points = shapely.points([site.x for site in sites], [site.y for site in sites]) if n else np.array([])
    if region_wkb is not None and n:
        region = shapely.from_wkb(bytes(region_wkb))
        shapely.prepare(region)
        in_region: list[bool | None] = [bool(v) for v in shapely.intersects(region, points)]
    else:
        in_region = [None] * n
    egon = list(matches) if matches is not None else [(None, None)] * n
    frame = pd.DataFrame({
        "source": [site.source for site in sites],
        "source_ids": [list(site.source_ids) for site in sites],
        "name": [site.name for site in sites],
        "operator": [site.operator for site in sites],
        "voltage_tag": [site.voltage_tag for site in sites],
        "max_v": pd.Series([site.max_v for site in sites], dtype="int64"),
        "mv_tagged": pd.Series([site.mv_tagged for site in sites], dtype=bool),
        "egon_bus_id": pd.Series([bus for bus, _ in egon], dtype="Int64"),
        "egon_distance_m": pd.Series([np.nan if d is None else d for _, d in egon], dtype=float),
        "in_region": pd.Series(in_region, dtype=object),
        "osm_ratings_mva": [list(r) if r else None for r in (ratings if ratings is not None else [()] * n)],
        "flags": [list(site.flags) + (["no_egon_bus"] if matches is not None and bus is None else [])
                  for site, (bus, _) in zip(sites, egon)],
        "x": pd.Series([site.x for site in sites], dtype=float),
        "y": pd.Series([site.y for site in sites], dtype=float),
        "area_m2": pd.Series([site.area_m2 for site in sites], dtype=float),
        "footprint_wkb": [site.footprint_wkb for site in sites],
    }, columns=list(SITE_COLUMNS))
    return frame


# ---------------------------------------------------------------------------------------------- ratings (P2)
def site_ratings(rows: pd.DataFrame, n_sites: int) -> list[tuple[float, ...]]:
    """The ratings (MVA) of the HV/MV transformers mapped in each site's footprint.

    A transformer counts if its highest tagged voltage is an HV level (110 kV or 60 kV; a 220/110 kV unit does
    not) and its ``rating`` parses (:func:`parse_rating_mva`).

    Args:
        rows: Rows of :meth:`MvSourceMixin.osm_transformer_ratings` (``site_index``, ``voltage``, ``rating``).
        n_sites: Number of sites (length of the result).

    Returns:
        Sorted ratings per site index (empty tuples where none is mapped).
    """
    ratings: list[list[float]] = [[] for _ in range(n_sites)]
    if rows is not None:
        for row in rows.to_dict("records"):
            voltages = parse_voltages(row.get("voltage"))
            if not voltages or max(voltages) not in HV_LEVELS_V:
                continue
            mva = parse_rating_mva(row.get("rating"))
            index = int(row["site_index"])
            if mva is not None and 0 <= index < n_sites:
                ratings[index].append(mva)
    return [tuple(sorted(r)) for r in ratings]


def known_units_mva(ratings: Sequence[float]) -> tuple[float, ...]:
    """The known HV/MV units of a substation: its OSM ratings if at least two units carry one, else ``()``."""
    values = sorted(float(r) for r in ratings if r is not None and float(r) > 0)
    return tuple(values) if len(values) >= 2 else ()
