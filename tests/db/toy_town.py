"""Synthetic "toy town" in the layout of the InfDB source tables, the input of pylovo's database tests.

With ``USE_INFDB=True`` (the default) ``pylovo-setup`` and ``pylovo-generate`` read their input from these
relations of the InfDB schemas (see :mod:`pylovo.infdb.infdb_client`):

* ``<INFDB_SOURCE_SCHEMA>.buildings``: buildings with use, floor areas, households, address, footprint and
  centroid; the LoD2 transformer-station buildings (``building_use_id`` ``31001_2523``) become transformer
  candidates,
* ``<INFDB_SOURCE_SCHEMA>.ways_per_connection``: the street segments, split at every junction and at every
  building connection point,
* ``<INFDB_SOURCE_SCHEMA>.connection_lines``: one line from each building centroid to its point on the street,
* ``<INFDB_SOURCE_SCHEMA>.classify_building_use(text)``: the use of an ALKIS building function code,
* ``<INFDB_OPENDATA_SCHEMA>.postcodes_germany``: the postcode polygons.

:func:`write_source_data` creates exactly these (the columns pylovo and its API read) and fills them with a
made-up town: postcode 99001 (a street grid with apartment blocks, mixed buildings with shops, a school, a
plant large enough for an MV connection, single-family houses, a LoD2 transformer station, a garage without
address, a building of unknown use, three residential buildings without a household count and a cycle path), the
neighbouring village 99002 (one street across the postcode border) and
99003, a postcode that only :func:`write_municipal_register` knows (no polygon, no buildings). Everything
is computed from the tables below (no randomness, no geodata files), in EPSG:25832 in the open North Sea west
of Heligoland, so no real building or OSM transformer lies near it.

``tests/db/build_test_database.py`` builds a complete test database from it; before its version 5 it places the
manual station :data:`MANUAL_STATION` with a rated power.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

SRID = 25832
X0, Y0 = 400_000.0, 6_000_000.0   # origin of the local coordinates (metres)

PLZ_TOWN, PLZ_VILLAGE, PLZ_NO_GEOMETRY = 99001, 99002, 99003
#: Postcode -> (name, local polygon x0, y0, x1, y1); 99003 has no polygon.
POSTCODES = {PLZ_TOWN: ("Toy Town", 0, 0, 1000, 600), PLZ_VILLAGE: ("Toy Village", 1000, 0, 1600, 600)}
AGS = {PLZ_TOWN: 99001000, PLZ_VILLAGE: 99002000, PLZ_NO_GEOMETRY: 99003000}
GENERATED_PLZ = sorted(POSTCODES)

#: name, klasse (InfDB road class), start, end (local metres, axis-parallel)
STREETS = [
    ("Hauptstraße", "Kreisstraße", (40, 300), (1560, 300)),
    ("Nordweg", "Gemeindestraße", (100, 500), (900, 500)),
    ("Südweg", "Gemeindestraße", (100, 100), (900, 100)),
    ("Westgasse", "Gemeindestraße", (100, 100), (100, 500)),
    ("Mittelgasse", "Gemeindestraße", (500, 100), (500, 500)),
    ("Ostgasse", "Gemeindestraße", (900, 100), (900, 500)),
    ("Dorfstraße", "Gemeindestraße", (1300, 120), (1300, 480)),
    ("Radweg", "Rad- und Fußweg", (700, 500), (700, 580)),  # left out by pylovo (klasse 'Rad- und Fußweg')
]


@dataclass(frozen=True)
class Kind:
    """A building type: footprint (along x across the street), floors, households and the InfDB classification."""

    along: float
    across: float
    floors: int
    households: int | None          # None: pylovo fills the count from the type and the residential floor area
    building_use: str
    building_use_id: str
    building_type: str | None
    nonresidential_floors: int = 0   # floors with non-residential use (from the ground floor up)
    address: bool = True


KINDS = {
    "SFH": Kind(10, 10, 2, 1, "Residential", "31001_1010", "SFH"),
    "TH": Kind(8, 10, 2, 1, "Residential", "31001_1010", "TH"),
    "MFH": Kind(14, 14, 3, 5, "Residential", "31001_1010", "MFH"),
    "AB": Kind(20, 16, 4, 12, "Residential", "31001_1010", "AB"),
    "MIXED": Kind(20, 16, 4, 9, "Mixed", "31001_1120", "AB", nonresidential_floors=1),   # shop on the ground floor
    "SHOP": Kind(24, 20, 1, 0, "Commercial", "31001_2050", None, nonresidential_floors=1),
    "SCHOOL": Kind(40, 20, 2, 0, "Public", "31001_3065", None, nonresidential_floors=2),
    "PLANT": Kind(50, 30, 2, 0, "Commercial", "31001_2100", None, nonresidential_floors=2),  # 3000 m2: MV-direct
    "STATION": Kind(4, 3, 1, 0, "Commercial", "31001_2523", None, nonresidential_floors=1, address=False),
    "GARAGE": Kind(6, 6, 1, 0, "Residential", "31001_2463", None, nonresidential_floors=1, address=False),
    "UNKNOWN": Kind(12, 10, 1, 0, "Unknown", "31001_9998", None, nonresidential_floors=1),  # use not classified
    # no household count in the InfDB (the fallback of PreprocessingMixin.set_building_peak_load)
    "MFH_NO_COUNT": Kind(14, 14, 3, None, "Residential", "31001_1010", "MFH"),
    "AB_NO_COUNT": Kind(20, 16, 4, None, "Residential", "31001_1010", "AB"),
    "UNTYPED_NO_COUNT": Kind(12, 10, 2, None, "Residential", "31001_1010", None),
}

#: street, side (+1 left of the street direction, -1 right), first position along the street, kinds in order
ROWS = [
    ("Hauptstraße", +1, 10, ["AB", "MIXED", "AB", "SHOP", "MIXED", "AB", "MIXED", "AB", "AB", "MIXED", "SHOP",
                             "AB", "MIXED", "AB", "AB", "MIXED", "AB", "AB", "MIXED", "AB"]),
    ("Hauptstraße", -1, 25, ["MFH", "SCHOOL", "MFH", "AB", "MFH", "SHOP", "MFH", "MFH", "AB", "MFH", "MFH", "AB",
                             "MFH", "MFH", "AB", "MFH"]),
    ("Nordweg", -1, 30, ["SFH"] * 24),
    ("Nordweg", +1, 40, ["TH"] * 10),
    ("Südweg", +1, 30, ["SFH"] * 24),
    ("Südweg", -1, 330, ["PLANT"]),
    ("Westgasse", -1, 30, ["SFH", "SFH", "SFH", "SFH", "SFH", "SFH", "SFH", "SFH"]),
    ("Mittelgasse", +1, 30, ["SFH", "STATION", "SFH", "SFH", "SFH", "SFH", "SFH", "SFH"]),
    ("Mittelgasse", -1, 30, ["SFH", "GARAGE", "SFH", "SFH", "SFH", "SFH", "SFH", "SFH"]),
    ("Ostgasse", -1, 30, ["MFH", "MFH", "MFH", "MFH", "MFH", "MFH"]),
    ("Hauptstraße", +1, 1010, ["SFH"] * 9),    # the village (beyond the postcode border at x = 1000)
    ("Hauptstraße", -1, 1020, ["SFH", "SFH", "SFH", "SFH", "SHOP", "SFH", "SFH"]),
    ("Dorfstraße", +1, 30, ["SFH"] * 10),
    ("Dorfstraße", -1, 30, ["SFH"] * 10),
    ("Ostgasse", +1, 30, ["UNKNOWN", "MFH_NO_COUNT", "AB_NO_COUNT", "UNTYPED_NO_COUNT"]),
]
GAP = 8.0             # metres between neighbouring buildings
SETBACK = 6.0         # metres from the street centre line to the building front
JUNCTION_CLEARANCE = 25.0  # no building faces a street within this distance of a junction or a postcode border

#: osm_id, local position and rated power (kVA) of the station placed by hand (transformer map, HTTP API) between
#: Nordweg and Hauptstraße; build_test_database.py adds it before version 5, which uses only the manual positions
MANUAL_STATION = ("manual/toy-town", (300.0, 475.0), 250)


@dataclass(frozen=True)
class Building:
    objectid: str
    kind: Kind
    street: str
    house_number: int
    centre: tuple[float, float]
    half: tuple[float, float]      # half extent in x and y
    foot: tuple[float, float]      # connection point on the street centre line
    plz: int

    @property
    def floor_area(self) -> float:
        return 4 * self.half[0] * self.half[1]


def _plz_of(x: float) -> int:
    return PLZ_TOWN if x < POSTCODES[PLZ_TOWN][3] else PLZ_VILLAGE


def _street(name: str):
    return next(s for s in STREETS if s[0] == name)


def _junctions(name: str) -> list[float]:
    """Positions along a street where another street starts, ends or crosses it, and the postcode border."""
    _, _, (ax, ay), (bx, by) = _street(name)
    horizontal = ay == by
    points = set()
    for other, _, (cx, cy), (dx, dy) in STREETS:
        if other == name or (cy == dy) == horizontal:
            continue
        if horizontal and min(cy, dy) <= ay <= max(cy, dy) and min(ax, bx) <= cx <= max(ax, bx):
            points.add(cx - ax)
        if not horizontal and min(cx, dx) <= ax <= max(cx, dx) and min(ay, by) <= cy <= max(ay, by):
            points.add(cy - ay)
    border = POSTCODES[PLZ_TOWN][3]
    if horizontal and min(ax, bx) < border < max(ax, bx):
        points.add(border - ax)
    return sorted(points)


def buildings() -> list[Building]:
    """All buildings, in a fixed order (the object ids number them)."""
    result = []
    numbers: dict[str, int] = {}
    for name, side, start, kinds in ROWS:
        _, _, (ax, ay), (bx, by) = _street(name)
        horizontal = ay == by
        length = abs(bx - ax) + abs(by - ay)
        blocked = _junctions(name) + [0.0, length]
        t = float(start)
        for kind_name in kinds:
            kind = KINDS[kind_name]
            while any(j - JUNCTION_CLEARANCE < t + kind.along and t < j + JUNCTION_CLEARANCE for j in blocked):
                t += 1.0
            mid = t + kind.along / 2
            offset = SETBACK + kind.across / 2
            if horizontal:
                foot = (ax + mid, ay)
                centre = (foot[0], ay + side * offset)
                half = (kind.along / 2, kind.across / 2)
            else:
                foot = (ax, ay + mid)
                centre = (ax - side * offset, foot[1])
                half = (kind.across / 2, kind.along / 2)
            number = numbers[name] = numbers.get(name, 0) + 1
            result.append(Building(f"TOY_{len(result) + 1:04d}", kind, name, number, centre, half, foot,
                                   _plz_of(centre[0])))
            t += kind.along + GAP
    return result


def connected(building: Building) -> bool:
    """Whether the building gets a connection line (the consumers with an address; not stations and garages)."""
    return building.kind.address


def street_segments() -> list[tuple[str, str, tuple[float, float], tuple[float, float]]]:
    """``(street, klasse, start, end)`` of the streets split at junctions, the border and connection points."""
    feet: dict[str, set[float]] = {}
    for b in buildings():
        if connected(b):
            _, _, (ax, ay), _ = _street(b.street)
            feet.setdefault(b.street, set()).add(abs(b.foot[0] - ax) + abs(b.foot[1] - ay))
    segments = []
    for name, klasse, (ax, ay), (bx, by) in STREETS:
        length = abs(bx - ax) + abs(by - ay)
        cuts = sorted({0.0, float(length), *_junctions(name), *feet.get(name, ())})
        dx, dy = (bx - ax) / length, (by - ay) / length
        for t0, t1 in pairwise(cuts):
            segments.append((name, klasse, (ax + dx * t0, ay + dy * t0), (ax + dx * t1, ay + dy * t1)))
    return segments


def _point(p):
    return f"{X0 + p[0]:.3f} {Y0 + p[1]:.3f}"


def _line(a, b) -> str:
    return f"SRID={SRID};LINESTRING({_point(a)}, {_point(b)})"


def _box(x0, y0, x1, y1, multi=True) -> str:
    ring = ", ".join(_point(p) for p in ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)))
    return f"SRID={SRID};MULTIPOLYGON((({ring})))" if multi else f"SRID={SRID};POLYGON(({ring}))"


SOURCE_DDL = """
CREATE SCHEMA {src};
CREATE SCHEMA IF NOT EXISTS {od};
CREATE TABLE {src}.buildings (
    id integer PRIMARY KEY, feature_id integer, objectid text NOT NULL UNIQUE, height double precision,
    floor_area double precision, floor_number integer, residential_floor_area double precision,
    nonresidential_floor_area double precision, mix_rule text, building_use text, building_use_id text,
    building_type text, occupants integer, households integer, construction_year text, postcode integer,
    address_street_id bigint, street text, house_number text, geom geometry(MultiPolygon, {srid}),
    centroid geometry(Point, {srid}), gemeindeschluessel text, changelog_id bigint, assigned_way_id text);
CREATE TABLE {src}.ways_per_connection (
    id bigint PRIMARY KEY, klasse text, name text, length_geo double precision, postcode integer, ags text,
    geom geometry(LineString, {srid}));
CREATE TABLE {src}.connection_lines (
    id bigint PRIMARY KEY, klasse text, length_geo double precision, postcode integer, ags text,
    building_objectid text, geom geometry(LineString, {srid}));
CREATE FUNCTION {src}.classify_building_use(building_use_id text) RETURNS text
    LANGUAGE sql IMMUTABLE AS $$
        SELECT CASE left(split_part(building_use_id, '_', 2), 1)
                   WHEN '1' THEN 'Residential' WHEN '2' THEN 'Commercial' WHEN '3' THEN 'Public' ELSE 'Unknown' END
    $$;
CREATE TABLE {od}.postcodes_germany (
    plz integer PRIMARY KEY, note text, qkm double precision, einwohner integer,
    geom geometry(MultiPolygon, {srid}));
-- as in the InfDB: the municipality keys are indexed, the postcodes are not
CREATE INDEX ON {src}.buildings (gemeindeschluessel);
CREATE INDEX ON {src}.ways_per_connection (ags);
CREATE INDEX ON {src}.connection_lines (ags);
"""


def write_source_data(cur, source_schema: str = "basedata", opendata_schema: str = "opendata") -> dict:
    """Create the InfDB source relations in ``source_schema`` and ``opendata_schema`` and fill them.

    The source schema must not exist yet; PostGIS must be installed. The tables get the municipality-key
    indexes of the InfDB and are analyzed. Nothing is committed.

    Returns:
        Counts of the written rows.
    """
    from psycopg2 import sql

    cur.execute(sql.SQL(SOURCE_DDL.replace("{srid}", str(SRID))).format(
        src=sql.Identifier(source_schema), od=sql.Identifier(opendata_schema)))
    table = lambda name, schema=source_schema: sql.Identifier(schema, name)
    population = {plz: 0 for plz in POSTCODES}
    rows, lines = [], []
    for i, b in enumerate(buildings(), start=1):
        k = b.kind
        nonres = b.floor_area * k.nonresidential_floors
        res = b.floor_area * (k.floors - k.nonresidential_floors)
        occupants = None if k.households is None else 2 * k.households
        population[b.plz] += occupants or 0
        street = b.street if k.address else None
        rows.append((i, 100_000 + i, b.objectid, 3.0 * k.floors, b.floor_area, k.floors, res, nonres,
                     k.building_use, k.building_use_id, k.building_type, occupants, k.households,
                     str(1950 + (i * 7) % 70), b.plz, STREETS.index(_street(b.street)) + 1, street,
                     str(b.house_number) if k.address else None,
                     _box(b.centre[0] - b.half[0], b.centre[1] - b.half[1], b.centre[0] + b.half[0],
                          b.centre[1] + b.half[1]),
                     f"SRID={SRID};POINT({_point(b.centre)})", str(AGS[b.plz])))
        if connected(b):
            lines.append((len(lines) + 1, "connection_line", abs(b.centre[0] - b.foot[0]) + abs(b.centre[1] - b.foot[1]),
                          b.plz, str(AGS[b.plz]), b.objectid, _line(b.centre, b.foot)))
    cur.executemany(sql.SQL("""
        INSERT INTO {} (id, feature_id, objectid, height, floor_area, floor_number, residential_floor_area,
            nonresidential_floor_area, building_use, building_use_id, building_type, occupants, households,
            construction_year, postcode, address_street_id, street, house_number, geom, centroid, gemeindeschluessel)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """).format(table("buildings")), rows)
    cur.executemany(sql.SQL("""INSERT INTO {} (id, klasse, length_geo, postcode, ags, building_objectid, geom)
                               VALUES (%s, %s, %s, %s, %s, %s, %s)""").format(table("connection_lines")), lines)
    ways = []
    for name, klasse, a, b in street_segments():
        plz = _plz_of((a[0] + b[0]) / 2)
        ways.append((len(ways) + 1, klasse, name, abs(b[0] - a[0]) + abs(b[1] - a[1]), plz, str(AGS[plz]), _line(a, b)))
    cur.executemany(sql.SQL("""INSERT INTO {} (id, klasse, name, length_geo, postcode, ags, geom)
                               VALUES (%s, %s, %s, %s, %s, %s, %s)""").format(table("ways_per_connection")), ways)
    cur.executemany(sql.SQL("INSERT INTO {} (plz, note, qkm, einwohner, geom) VALUES (%s, %s, %s, %s, %s)").format(
        table("postcodes_germany", opendata_schema)),
        [(plz, f"{plz} {name}", (x1 - x0) * (y1 - y0) / 1e6, population[plz], _box(x0, y0, x1, y1))
         for plz, (name, x0, y0, x1, y1) in POSTCODES.items()])
    # planner statistics now, as on a loaded InfDB (the API's coverage engine reads reltuples)
    cur.execute(sql.SQL("ANALYZE {}, {}, {}, {}").format(
        table("buildings"), table("ways_per_connection"), table("connection_lines"),
        table("postcodes_germany", opendata_schema)))
    return {"buildings": len(rows), "connection_lines": len(lines), "street_segments": len(ways),
            "postcodes": len(POSTCODES)}


def write_municipal_register(cur) -> None:
    """Add the toy postcodes to ``pylovo.municipal_register`` (filled by ``pylovo-setup`` from data files).

    99003 is registered without a polygon: the API reports it as a postcode without input.
    """
    centres = {plz: ((x0 + x1) / 2, (y0 + y1) / 2) for plz, (_, x0, y0, x1, y1) in POSTCODES.items()}
    centres[PLZ_NO_GEOMETRY] = (1900, 300)
    names = {PLZ_TOWN: "Toy Town", PLZ_VILLAGE: "Toy Village", PLZ_NO_GEOMETRY: "Toy Hamlet"}
    for plz, (x, y) in centres.items():
        area = (POSTCODES[plz][3] - POSTCODES[plz][1]) * (POSTCODES[plz][4] - POSTCODES[plz][2]) / 1e6 \
            if plz in POSTCODES else 0.2
        pop = sum(2 * (b.kind.households or 0) for b in buildings() if b.plz == plz)
        cur.execute(f"""
            INSERT INTO pylovo.municipal_register (plz, pop, area, lat, lon, ags, name_city, fed_state, regio7, regio5,
                                                   pop_den)
            SELECT %(plz)s, %(pop)s, %(area)s, ST_Y(p), ST_X(p), %(ags)s, %(name)s, 99, 75, 54, %(pop)s / %(area)s
            FROM (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%(x)s, %(y)s), {SRID}), 4326) AS p) t
        """, {"plz": plz, "pop": pop, "area": area, "ags": AGS[plz], "name": names[plz], "x": X0 + x, "y": Y0 + y})
