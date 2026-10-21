"""MV stage parameters: the ``MV_*`` YAML keys, the constants and their coercion rules.

* :data:`YAML_KEYS`: the ``MV_*`` keys of ``config_generation_mv.yaml``. :mod:`pylovo.config_loader` reads each of
  them with :func:`coerce_mv_value`; a missing key takes its default (the shipped value). ``pylovo-mv generate
  --set KEY=VALUE`` changes them per variant.
* :data:`CONSTANTS`: the constants of the stage. They are not YAML keys, but they are part of the snapshot of an MV
  variant; research code changes them with
  :meth:`MvParameters.replace <pylovo.mv_grid.parameters.MvParameters.replace>`.

Each parameter is described by a :class:`Spec`; :class:`~pylovo.mv_grid.parameters.MvParameters` has one field per
parameter (``Spec.field``). This module is pure: it imports only the standard library and ``yaml``, because
:mod:`pylovo.config_loader` imports it.
"""
from __future__ import annotations

import copy
import difflib
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping

import yaml

#: Version of the MV stage code; bumped whenever results change. Stored in every MV variant snapshot.
#: 2: MV customer halls count one storey, customers below ``customer_min_mv_kw`` hang on the nearest station.
#: 3: nominal voltage per district (``MV_NOMINAL_VOLTAGE_KV`` auto), station-busbar customers only within
#: ``busbar_customer_max_m``, stations they overload upsized in the MV model (and in ``pylovo-mv couple``).
#: 4: load-only planning by default, optional generation and feed-in cases, heavier starting conductors.
#: 5: rural ring members count ``1 / rural_ring_factor``, busbar legs are cables along the streets with exits within
#: ``busbar_exit_radius_m``, isolated rural stations hang on stubs after the rings are solved (satellites).
#: 6: ``MV_OVERHEAD_LINES`` osm (default): overhead lines only along the OSM MV lines of the routing graph.
#: 7: supply areas without capacity rebalancing (``rebalance_max_iter`` 0) and the option ``MV_SUPPLY_AREAS``
#: municipal; stub, radial and T-off limits on the true route length; larger conductors before topology changes in
#: the repair loop (8 rounds).
#: 8: stub trees up to 4 km, shortened by their load (``stub_max_kva_km``); normally-open links between the rings of
#: one district (``link_max_length_m``).
#: 9: the links reach by the settlement type of the PLZ (``link_max_length_m`` per type, a link takes the shorter
#: reach of its ends), and stub ends take links to another feeder (``link_stub_ends``).
#: 10: HV/MV substations inside thermal or heating plants are kept, and ``substation=transmission`` without a voltage tag
#: counts as HV (OSM substation filter).
#: 11: ``MV_NOMINAL_VOLTAGE_KV`` auto by municipality: a district gets 10 kV when at least half of its installed
#: capacity lies in municipalities of at least ``mv_10kv_min_density_mva_km2`` (now 5 MVA/km²), not by its own cell.
#: 12: a stub tree takes the true street length of each extension into its depth, also where the OSM-discounted
#: routing cost would already have been within the limit.
#: 13: repairs: a ring split keeps parallel cables and larger conductors only on the lines that run on the repaired
#: line, caps its rings by all the nodes it solves again (stubs included), and ring checks and open points use the
#: upgraded conductors; ``pylovo-mv couple`` splits a station rating outside pylovo's lists into units of
#: ``rating / units``.
MV_STAGE_VERSION: int = 13

KINDS: tuple[str, ...] = ("bool", "int", "float", "str", "nullable_str", "float_tuple", "str_tuple",
                          "mapping", "voltage", "nullable_float")

#: Keys of ``link_max_length_m`` by the settlement type of a PLZ (pylovo LV stage: 1 rural, 2 semi-urban, 3 urban).
SETTLEMENT_TYPE_KEYS: dict[int, str] = {1: "rural", 2: "semi_urban", 3: "urban"}
#: Mappings that also take one number, which then applies to every key (their former single value).
BROADCAST_MAPPINGS: frozenset[str] = frozenset({"link_max_length_m"})

#: ``MV_NOMINAL_VOLTAGE_KV`` value of the density rule: a district gets 10 kV when at least half of its installed
#: MV/LV capacity lies in municipalities whose installed capacity per km² reaches ``mv_10kv_min_density_mva_km2``
#: (a district without installed capacity: its own density), else 20 kV.
AUTO_VOLTAGE: str = "auto"


@dataclass(frozen=True)
class Spec:
    """Definition of one MV parameter.

    Attributes:
        name: YAML key (``MV_MAX_STATIONS_PER_RING``) or constant name (``stub_max_kva``).
        field: Field name in :class:`~pylovo.mv_grid.parameters.MvParameters`; empty for ``MV_GRID_GENERATION``,
            which is an execution flag and no parameter.
        default: Default in canonical form (the shipped value of a YAML key).
        kind: One of :data:`KINDS`.
        choices: Allowed numbers of a ``voltage`` (the nominal voltages besides ``auto``).
        bounds: Inclusive lower and upper bound (``None`` = open) of a number, of the elements of a ``float_tuple``,
            of the numbers in a ``mapping``, or of the length of a ``str``.
        group: Group of the MV variant snapshot; empty = not in the snapshot.
        source: Source of the default, or the reason for it.
        key: Key inside the snapshot group; empty = the field name.
    """

    name: str
    field: str
    default: Any
    kind: str
    choices: tuple = ()
    bounds: tuple[float | None, float | None] = (None, None)
    group: str = ""
    source: str = ""
    key: str = ""

    @property
    def path(self) -> tuple[str, ...]:
        """Path of the value in the MV variant snapshot; empty if it is not in the snapshot."""
        if not self.group:
            return ()
        return (self.group, self.key or self.field)


_YAML_SPECS: tuple[Spec, ...] = (
    Spec("MV_GRID_GENERATION", "", False, "bool",
         source="execution flag of pylovo-generate (like ANALYZE_GRIDS), in no snapshot"),
    Spec("MV_VARIANT_ID", "variant_id", "1", "str", bounds=(1, 10),
         source="as VERSION_ID; the key of pylovo.mv_variant together with the LV version"),
    Spec("MV_VARIANT_COMMENT", "comment", "default", "str",
         source="as VERSION_COMMENT; stored once with a new MV variant"),
    Spec("MV_NOMINAL_VOLTAGE_KV", "vn_kv", AUTO_VOLTAGE, "voltage", choices=(20.0, 10.0), group="voltage",
         source="auto: 10 kV in districts with at least half of their installed capacity in municipalities with an "
                "installed density of at least mv_10kv_min_density_mva_km2, else 20 kV (dense city grids run at "
                "10 kV, rural grids at 20 kV, Harnisch 2016); the stored LV station "
                "transformers are 20/0.4 kV and are re-typed to 10 kV by pylovo-mv couple"),
    Spec("MV_MAX_STATIONS_PER_RING", "max_stations_per_ring", 20, "int", bounds=(1, 100), group="topology",
         source="Ding0 and Heuck; 5 to 30 stations per ring in a large-city grid study (mean 15); urban members "
                "count 1, rural members 1 / rural_ring_factor"),
    Spec("MV_STANDARD_CABLE", "cable_type", None, "nullable_str", group="equipment", key="cable",
         source="null = NA2XS2Y 1x240 RM/25, starting cable for both 10 and 20 kV; narrower types remain selectable"),
    Spec("MV_OVERHEAD_LINE", "overhead_type", None, "nullable_str", group="equipment", key="overhead",
         source="null = 122-AL1/20-ST1A, starting overhead conductor for both 10 and 20 kV"),
    Spec("MV_LINE_PLANNING_UTILIZATION", "line_utilization", 0.6, "float", bounds=(0.1, 1.0), group="loading",
         source="dena 2012; Harnisch 2016; Wintzek 2021 (SimBench 0.5)"),
    Spec("MV_MAX_VOLTAGE_DROP_PERCENT", "max_drop_percent", 5.0, "float", bounds=(0.5, 20.0), group="voltage",
         source="Harnisch 2016; SimBench; Ding0"),
)


def _constant(name: str, default: Any, kind: str, group: str, source: str, *, bounds=(None, None),
              key: str = "") -> Spec:
    return Spec(name, name, default, kind, bounds=bounds, group=group, source=source, key=key)


_POSITIVE = (0.0, None)

_CONSTANT_SPECS: tuple[Spec, ...] = (
    # voltage band (DIN EN 50160 split between HV/MV, MV, MV/LV and LV)
    _constant("busbar_vm_pu", 1.02, "float", "voltage", bounds=(0.9, 1.1),
              source="OLTC setpoint at the MV busbar: Harnisch 2016 (102.5 ± 1.5 %), Wintzek 2021 (102 ± 2 %)"),
    _constant("busbar_band_pu", 0.015, "float", "voltage", bounds=(0.0, 0.1),
              source="OLTC control band at the MV busbar, Harnisch 2016 (± 1.5 %)"),
    _constant("max_drop_n1_percent", 10.0, "float", "voltage", bounds=(0.5, 30.0),
              source="(n-1): ring fed from one side; Ding0; SimBench"),
    _constant("mv_10kv_min_density_mva_km2", 5.0, "float", "voltage", bounds=_POSITIVE,
              source="MV_NOMINAL_VOLTAGE_KV auto: districts whose installed capacity lies mostly in municipalities "
                     "with at least this installed MV/LV capacity per km² of their area in the planning region "
                     "(station ratings plus MV customer peaks / 0.95) are 10 kV, the others 20 kV. § 23c EnWG (2025): "
                     "a large-city 10 kV DSO has 8.1 MVA/km² of real capacity, large Bavarian 20 kV DSOs 0.3 to 0.6. "
                     "pylovo installs about 0.6 of the real capacity (2.3 of 3.9 GVA in the large city), so the large "
                     "city comes to 8.6 MVA/km² and the densest towns of the Bavarian test regions (20 kV) to 2.5 to "
                     "3.4"),
    # loading
    _constant("n1_utilization", 1.0, "float", "loading", bounds=(0.1, 2.0),
              source="lines in the (n-1) cases, dena 2012"),
    _constant("hv_mv_n1_utilization", 1.0, "float", "loading", bounds=(0.1, 2.0),
              source="HV/MV transformers with one unit out, dena 2012"),
    # topology
    _constant("max_half_ring_length_m", 28000.0, "float", "topology", bounds=(100.0, None),
              source="Ding0 configuration; FGH ring length up to 60 km"),
    _constant("rural_ring_factor", 2.0, "float", "topology", bounds=(1.0, 10.0),
              source="rural ring members count 1 / factor against MV_MAX_STATIONS_PER_RING, so rural rings hold "
                     "twice as many members: rural feeders carry 11 to 28 supply points (SimBench; Almomani & "
                     "Witzmann 2024), urban ones about 10 (large-city grid study); 1 = all members count 1"),
    _constant("stub_max_kva", 1000.0, "float", "topology", bounds=_POSITIVE,
              source="size of a typical mobile emergency generator, Amme et al. 2018 p. 10 (after LEW and FGH 2008)"),
    _constant("stub_max_length_m", 4000.0, "float", "topology", bounds=_POSITIVE,
              source="longest stub tree: the upper end of the 2 to 4 km of load stubs in real grids (Tran et al. "
                     "2019)"),
    _constant("stub_max_kva_km", 2000.0, "float", "topology", bounds=_POSITIVE,
              source="largest product of stub tree load (kVA) and length (km): the outage energy of a stub grows with "
                     "both; 2 MVA km gives 2 km at the 1 MVA of an emergency generator (Consentec 2006, Amme et al. "
                     "2018) and 4 km up to 0.5 MVA (derived rule)"),
    _constant("stub_min_offset_m", 100.0, "float", "topology", bounds=_POSITIVE,
              source="only nodes more than this off the ring become stubs; Amme et al. 2018"),
    _constant("tie_max_length_m", 2000.0, "float", "topology", bounds=_POSITIVE,
              source="normally-open ties to a neighbouring district [assumption]"),
    _constant("link_max_length_m", {"rural": 1000.0, "semi_urban": 750.0, "urban": 500.0}, "mapping", "topology",
              bounds=_POSITIVE,
              source="longest normally-open link inside a district by the settlement type of the PLZ of its ends "
                     "(pylovo settlement_type 1 rural, 2 semi-urban, 3 urban): about three times the median distance "
                     "between neighbouring stations (320, 250 and 180 m in the test region); a link takes the shorter "
                     "reach of its two ends, an end without a type the urban one (assumption); one number applies "
                     "to every type"),
    _constant("link_stub_ends", True, "bool", "topology",
              source="after the links between rings, each stub end (a stub station with one MV line) takes at most "
                     "one normally-open link within its reach to a station of another feeder of its district, a "
                     "second infeed as many real rural stations have"),
    _constant("local_search_rounds", 50, "int", "topology", bounds=(0, None),
              source="pass limit of the granular local search (Ding0 used 10 fixed rounds); 0 = no local search"),
    _constant("neighbours", 15, "int", "topology", bounds=(1, None),
              source="neighbour list length of the granular local search"),
    _constant("ls_time_guard_s", 120.0, "float", "topology", bounds=_POSITIVE,
              source="wall-clock safety net of the local search (flagged when hit)"),
    _constant("repair_rounds", 8, "int", "topology", bounds=(0, 20),
              source="rounds of the electrical repair loop (an overhead line takes up to three rounds to reach the "
                     "largest conductor)"),
    # loads
    _constant("dedicated_feeder_kw", 5500.0, "float", "loads", bounds=_POSITIVE,
              source="eGon-data MV customers up to 5.5 MW; 20 kV connection rules 0.2 to 5.5 MW"),
    _constant("hv_customer_kw", 20000.0, "float", "loads", bounds=_POSITIVE,
              source="eGon-data HV/MV level up to 20 MW; larger customers are HV customers (excluded)"),
    _constant("customer_hall_min_footprint_m2", 2000.0, "nullable_float", "loads", bounds=_POSITIVE,
              source="MV customer buildings with at least this footprint are halls: their non-residential floor area "
                     "for the MV load is one storey, min(footprint, floor area); the building data count large "
                     "single-storey halls with storeys derived from their height [assumption]; null: no hall rule"),
    _constant("customer_min_mv_kw", 200.0, "float", "loads", bounds=_POSITIVE,
              source="eGon-data: MV level above 200 kW building peak (the own peak: installed load after the hall "
                     "rule, factor 1); smaller MV customers are station-busbar customers of the nearest MV/LV station"),
    _constant("busbar_customer_max_m", 1000.0, "nullable_float", "loads", bounds=_POSITIVE,
              source="a small MV customer hangs on the busbar of its nearest MV/LV station only within this "
                     "straight-line distance (pylovo's default MAX_GREENFIELD_TRAFO_DISTANCE), else it keeps an own "
                     "MV node (flag remote_small_customer); null: no limit"),
    # equipment
    _constant("hv_mv_sizes_mva", (25.0, 31.5, 40.0, 63.0), "float_tuple", "equipment", key="hv_mv_types",
              bounds=(1.0, 1000.0), source="Meinecke 2019; Harnisch 2016 (31.5 MVA interpolated)"),
    _constant("hv_mv_units", 2, "int", "equipment", bounds=(1, 10),
              source="two HV/MV transformers per substation, Harnisch 2016"),
    _constant("max_parallel_cables", 2, "int", "equipment", bounds=(1, 10),
              source="parallel cables only as a repair fallback"),
    # routing and the regional routing graph
    _constant("bridge_detour", 1.3, "float", "routing", bounds=(1.0, 3.0),
              source="straight bridges between graph islands (detour factor of Ding0)"),
    _constant("long_connector_m", 300.0, "float", "routing", bounds=_POSITIVE,
              source="connectors from a node to the street longer than this are reported and count bridge_detour "
                     "times their length"),
    _constant("osm_line_snap_m", 100.0, "float", "routing", bounds=(0.0, 1000.0),
              source="a loose end of an OSM MV line joins the nearest street vertex within "
                     "this distance by a straight cable connector (a riser pole and its cable); OSM lines also join "
                     "the streets where they cross them [assumption]"),
    _constant("osm_line_cost_factor", 0.7, "float", "routing", bounds=(0.05, 1.0),
              source="an OSM MV line costs this factor times its length when routes and "
                     "rings are planned, because an existing overhead corridor needs no new trench, so rings follow "
                     "the corridors; lengths and electrics use the true length. 0.7 gave overhead circuit km within "
                     "the § 23c range of the test regions, 0.5 above it [assumption; 1 = no preference]"),
    _constant("busbar_exit_radius_m", 200.0, "float", "routing", bounds=(0.0, 2000.0),
              source="the HV/MV busbar gets straight cable connectors to every street vertex within this distance, "
                     "so its feeders leave in several directions instead of one street [assumption]; 0 = only the "
                     "attachment of the substation"),
    _constant("graph_excluded_klasse", ("Bundesautobahn",), "str_tuple", "routing",
              source="no MV lines along motorways (klasse of the street graph)"),
    _constant("graph_snap_tolerance_m", 0.25, "float", "routing", bounds=(0.0, 10.0),
              source="endpoint snapping of the street graph"),
    _constant("subgraph_buffer_m", 2000.0, "float", "routing", bounds=_POSITIVE,
              source="buffer of the district subgraph around the hull of its nodes"),
    _constant("region_buffer_m", 5000.0, "float", "sources", bounds=_POSITIVE,
              source="search buffer for substations and graph municipalities around the planning region"),
    # settlements (building-based urban and rural areas)
    _constant("settlement_buffer_m", 50.0, "float", "settlements", key="buffer_m", bounds=_POSITIVE,
              source="address buildings within this distance of a street edge count for it"),
    _constant("settlement_spacing_m", 100.0, "float", "settlements", key="spacing_m", bounds=_POSITIVE,
              source="street length per building below which an edge is in a settlement"),
    _constant("settlement_min_buildings", 2, "int", "settlements", key="min_buildings", bounds=(1, None),
              source="fewest address buildings of a settlement edge"),
    _constant("urban_min_buildings", 1000, "int", "settlements", bounds=(1, None),
              source="settlements with at least this many address buildings are urban (a small town)"),
    _constant("settlement_polygon_buffer_m", 75.0, "float", "settlements", key="polygon_buffer_m", bounds=_POSITIVE,
              source="buffer of the settlement polygons around their edges"),
    # HV/MV substations and supply areas
    _constant("substation_merge_m", 75.0, "float", "sources", bounds=_POSITIVE,
              source="substation candidates closer than this are one site, Hülk et al. 2017"),
    _constant("substation_drop_m", 150.0, "float", "sources", bounds=_POSITIVE,
              source="duplicate sites closer than this are dropped"),
    _constant("ehv_partner_m", 1000.0, "float", "sources", bounds=_POSITIVE,
              source="EHV sites without an MV tag are dropped when an HV/MV site lies within this distance"),
    _constant("substation_max_attach_m", 2000.0, "float", "sources", bounds=_POSITIVE,
              source="longest connection of a substation busbar to the street graph"),
    _constant("egon_match_m", 300.0, "float", "sources", bounds=_POSITIVE,
              source="cross-check of OSM sites against eGon-data substations"),
    # MV/LV taps and LV verdicts
    _constant("mvlv_tap_step_percent", 2.5, "float", "taps", key="step_percent", bounds=(0.1, 10.0),
              source="off-load tap step of the MV/LV transformers (pandapower and SimBench types)"),
    _constant("mvlv_tap_min", -2, "int", "taps", key="min", bounds=(-10, 0),
              source="lowest MV/LV tap position"),
    _constant("mvlv_tap_max", 2, "int", "taps", key="max", bounds=(0, 10),
              source="highest MV/LV tap position"),
    _constant("lv_design_tolerance_pu", 0.002, "float", "taps", bounds=(0.0, 0.1),
              source="tolerance of the LV busbar below LV_REFERENCE_VOLTAGE_PU of the LV version"),
)

#: The ``MV_*`` keys of ``config_generation_mv.yaml`` by name.
YAML_KEYS: dict[str, Spec] = {spec.name: spec for spec in _YAML_SPECS}
#: The constants of the MV stage by name (= their field name in MvParameters).
CONSTANTS: dict[str, Spec] = {spec.name: spec for spec in _CONSTANT_SPECS}

#: YAML keys that ``--set`` does not change, with the option to use instead.
NOT_SETTABLE: dict[str, str] = {
    "MV_GRID_GENERATION": "it is the switch of pylovo-generate (use pylovo-generate --mv)",
    "MV_VARIANT_ID": "use --variant",
    "MV_VARIANT_COMMENT": "use --comment",
}

_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]+")


def spec_of(name: str) -> Spec:
    """The :class:`Spec` of a YAML key or constant name.

    Raises:
        ValueError: If ``name`` is neither (with the closest names as a hint).
    """
    spec = YAML_KEYS.get(name) or CONSTANTS.get(name)
    if spec is None:
        close = difflib.get_close_matches(name, [*YAML_KEYS, *CONSTANTS], n=3)
        hint = f" (did you mean {', '.join(close)}?)" if close else ""
        raise ValueError(f"unknown MV parameter {name!r}{hint}")
    return spec


def _fmt(number: float) -> str:
    return str(number) if isinstance(number, int) else f"{number:g}"


def _error(spec: Spec, expected: str, value: Any) -> ValueError:
    return ValueError(f"{spec.name} must be {expected}, got {value!r}")


def _check_bounds(spec: Spec, number: float, value: Any) -> None:
    low, high = spec.bounds
    if (low is not None and number < low) or (high is not None and number > high):
        if low is not None and high is not None:
            expected = f"between {_fmt(low)} and {_fmt(high)}"
        elif low is not None:
            expected = f"at least {_fmt(low)}"
        else:
            expected = f"at most {_fmt(high)}"
        raise _error(spec, expected, value)


def _float(spec: Spec, value: Any, original: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _error(spec, "a number", original)
    number = float(value)
    _check_bounds(spec, number, original)
    return number


def _mapping(spec: Spec, value: Any, template: Mapping[str, Any], original: Any) -> dict[str, Any]:
    """``value`` with exactly the keys of ``template`` (recursively) and numbers as leaves, in template order."""
    if not isinstance(value, Mapping) or set(map(str, value)) != set(template):
        raise _error(spec, f"a mapping with the keys {', '.join(template)}", original)
    value = {str(k): v for k, v in value.items()}
    return {key: (_mapping(spec, value[key], sub, original) if isinstance(sub, Mapping)
                  else _float(spec, value[key], original))
            for key, sub in template.items()}


def coerce_mv_value(name: str, value: Any) -> Any:
    """Check a value of a YAML key or constant and return it in canonical form.

    Canonical forms: ``float`` for numbers of kind float (``20`` becomes ``20.0``), ``int`` for integers, a
    sorted tuple for ``float_tuple`` and ``str_tuple`` values (a single text is a one-element tuple), a new
    ``dict`` in the key order of the default for mappings (for those of :data:`BROADCAST_MAPPINGS` one number
    applies to every key), ``"auto"`` or a ``float`` for the nominal voltage, and ``None`` or a ``float`` for
    ``nullable_float`` (``null`` switches the rule off).

    Raises:
        ValueError: ``"<name> must be ..., got <value>"`` if the value does not fit, or if ``name`` is unknown.
    """
    spec = spec_of(name)
    kind = spec.kind
    if kind == "bool":
        if not isinstance(value, bool):
            raise _error(spec, "True or False", value)
        result: Any = value
    elif kind == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (
                isinstance(value, float) and not value.is_integer()):
            raise _error(spec, "an integer", value)
        result = int(value)
        _check_bounds(spec, result, value)
    elif kind == "float":
        result = _float(spec, value, value)
    elif kind == "str":
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise _error(spec, "a text (quote it in YAML)", value)
        result = str(value)
        low, high = spec.bounds
        if (low is not None and len(result) < low) or (high is not None and len(result) > high):
            raise _error(spec, f"a text of {_fmt(low or 0)} to {_fmt(high)} characters" if high is not None
                         else f"a text of at least {_fmt(low)} characters", value)
        if spec.name == "MV_VARIANT_ID" and not _ID_PATTERN.fullmatch(result):
            raise _error(spec, "made of letters, digits, '_', '.' and '-'", value)
    elif kind == "nullable_str":
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise _error(spec, "a name or null", value)
        result = value
    elif kind == "nullable_float":
        result = None if value is None else _float(spec, value, value)
    elif kind == "float_tuple":
        if isinstance(value, (bool, str)) or not isinstance(value, (list, tuple)) or not value:
            raise _error(spec, "a non-empty list of numbers", value)
        numbers = [_float(spec, item, value) for item in value]
        if len(set(numbers)) != len(numbers):
            raise _error(spec, "a list of different numbers", value)
        result = tuple(sorted(numbers))
    elif kind == "str_tuple":
        items = [value] if isinstance(value, str) else value
        if not isinstance(items, (list, tuple)) or not all(isinstance(i, str) and i.strip() for i in items):
            raise _error(spec, "a list of names", value)
        result = tuple(sorted(set(items)))
    elif kind == "mapping":
        if spec.name in BROADCAST_MAPPINGS and isinstance(value, (int, float)) and not isinstance(value, bool):
            value = {key: value for key in spec.default}
        result = _mapping(spec, value, spec.default, value)
    elif kind == "voltage":
        if isinstance(value, str) and value.strip().casefold() == AUTO_VOLTAGE:
            result = AUTO_VOLTAGE
        elif (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
              or float(value) not in spec.choices):
            raise _error(spec, "one of " + ", ".join([AUTO_VOLTAGE, *(_fmt(c) for c in spec.choices)]), value)
        else:
            result = float(value)
    else:   # pragma: no cover - a Spec with an unknown kind is a programming error
        raise AssertionError(f"unknown kind {kind!r} of {spec.name}")
    return result


def default_value(name: str) -> Any:
    """A copy of the default of a YAML key or constant (mappings are mutable)."""
    return copy.deepcopy(spec_of(name).default)


def parse_assignment(text: str) -> tuple[str, Any]:
    """Parse one ``--set`` argument ``KEY=VALUE`` into the name and its coerced value.

    KEY is an ``MV_*`` YAML key, except :data:`NOT_SETTABLE` (the constants are changed in Python with
    :meth:`MvParameters.replace <pylovo.mv_grid.parameters.MvParameters.replace>`). VALUE is read with
    ``yaml.safe_load`` (``null``, numbers, ``[25, 40]``, ``{lv: 100, ...}``; plain text stays text) and then coerced
    with :func:`coerce_mv_value`.

    Raises:
        ValueError: For a malformed argument, an unknown or excluded key, or a value that does not fit.
    """
    name, sep, raw = text.partition("=")
    name = name.strip()
    if not sep or not name:
        raise ValueError(f"--set takes KEY=VALUE, got {text!r}")
    if name in NOT_SETTABLE:
        raise ValueError(f"{name} cannot be changed with --set: {NOT_SETTABLE[name]}")
    if name in CONSTANTS:
        raise ValueError(f"{name} is a constant of the MV stage, not a key of config_generation_mv.yaml: change it in "
                         f"Python with MvParameters.replace")
    if name not in YAML_KEYS:
        close = difflib.get_close_matches(name, [key for key in YAML_KEYS if key not in NOT_SETTABLE], n=3)
        hint = f" (did you mean {', '.join(close)}?)" if close else ""
        raise ValueError(f"unknown MV key {name!r}{hint}")
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"{name}: {raw!r} is not a valid YAML value") from exc
    return name, coerce_mv_value(name, value)
