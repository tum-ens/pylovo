"""Full-load hours of the BDEW standard load profiles (``LOAD_CLASSES.load_profiles`` of config_generation_lv.yaml).

The profiles are typical days (season x weekday, Saturday, Sunday) in quarter hours. A year takes the typical day of
each date by the BDEW seasons (winter 1 Nov - 20 Mar, transition 21 Mar - 14 May and 15 Sep - 31 Oct, summer 15 May -
14 Sep); the nationwide public holidays count as Sundays. The full-load hours are the annual energy over the largest
quarter-hour value, here the mean of the years 2015 to 2025. The H0 profile is used without its dynamisation.

Input: ``selp_series.csv`` of oemof demandlib (MIT licence; ``src/oemof/demand/bdew/bdew_data/selp_series.csv``),
which holds the typical days of H0, G0-G6 and L0-L2 (columns ``period``, ``weekday`` 1-7 and one per profile).
It is not part of this repository. Run from the repository root::

    uv run python docs/scripts/slp_full_load_hours.py path/to/selp_series.csv
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
from collections import defaultdict
from pathlib import Path

PROFILES = ("h0", "g0", "g1", "g2", "g3", "g4", "g5", "g6", "l0", "l1", "l2")
YEARS = range(2015, 2026)


def easter(year: int) -> dt.date:
    """Easter Sunday (Gregorian calendar, anonymous algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    lam = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lam) // 451
    return dt.date(year, (h + lam - 7 * m + 114) // 31, (h + lam - 7 * m + 114) % 31 + 1)


def holidays(year: int) -> set[dt.date]:
    """The nationwide public holidays of Germany."""
    e = easter(year)
    return {dt.date(year, 1, 1), e - dt.timedelta(days=2), e + dt.timedelta(days=1), dt.date(year, 5, 1),
            e + dt.timedelta(days=39), e + dt.timedelta(days=50), dt.date(year, 10, 3), dt.date(year, 12, 25),
            dt.date(year, 12, 26)}


def period(day: dt.date) -> str:
    """The BDEW season of a date."""
    md = (day.month, day.day)
    if (5, 15) <= md <= (9, 14):
        return "summer"
    if (3, 21) <= md <= (5, 14) or (9, 15) <= md <= (10, 31):
        return "transition"
    return "winter"


def full_load_hours(path: Path) -> dict[str, float]:
    """Mean full-load hours of each profile over :data:`YEARS`."""
    typical = defaultdict(lambda: defaultdict(list))
    with open(path, encoding="utf-8") as file:
        for row in csv.DictReader(file):
            for p in PROFILES:
                typical[(row["period"], int(row["weekday"]))][p].append(float(row[p]))
    result = {}
    for p in PROFILES:
        peak = max(max(day[p]) for day in typical.values())
        hours = []
        for year in YEARS:
            free, day, energy = holidays(year), dt.date(year, 1, 1), 0.0
            while day.year == year:
                weekday = 7 if day in free else day.isoweekday()
                energy += sum(typical[(period(day), weekday)][p]) * 0.25
                day += dt.timedelta(days=1)
            hours.append(energy / peak)
        result[p.upper()] = sum(hours) / len(hours)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("selp_series", type=Path, help="selp_series.csv of oemof demandlib")
    args = parser.parse_args()
    hours = full_load_hours(args.selp_series)
    print("{" + ", ".join(f"{p}: {round(h)}" for p, h in hours.items() if p != "H0") + "}")


if __name__ == "__main__":
    main()
