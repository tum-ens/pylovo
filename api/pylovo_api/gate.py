"""Generate gate: which of the requested PLZ ``pylovo-generate`` may run.

Used by ``GET /api/jobs/generate/check`` (what will happen) and ``POST /api/jobs/generate``
(enforced on the server, so a stale browser tab cannot start a doomed job). Analyse, export,
delete and every results view are never gated.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from pylovo_api import db
from pylovo_api.coverage import InputCoverage, brief, read_flags


def generated_plz(version_id: str, plz_list: list[int]) -> set[int]:
    rows = db.fetch_all("SELECT postcode_result_plz AS plz FROM pylovo.postcode_result WHERE version_id = %s "
                        "AND postcode_result_plz = ANY(%s)", (str(version_id), plz_list))
    return {r["plz"] for r in rows}


def register_by_ags(plz_list: list[int]) -> dict[int, dict[str, Any]]:
    """Every municipality of these PLZ with all its register PLZ (for the ``--ags`` decision)."""
    rows = db.fetch_all(
        """SELECT mr.ags, max(mr.name_city) AS name_city, array_agg(DISTINCT mr.plz ORDER BY mr.plz) AS plz
           FROM pylovo.municipal_register mr
           WHERE mr.ags IN (SELECT ags FROM pylovo.municipal_register WHERE plz = ANY(%s))
           GROUP BY mr.ags ORDER BY mr.ags""", (plz_list,))
    return {int(r["ags"]): {"name_city": r["name_city"], "plz": list(r["plz"])} for r in rows}


def classify(plz_list: list[int], cov: InputCoverage, version_id: str, wait_s: float) -> dict[str, Any]:
    """InputStatus and generate state (new | exists | blocked | unverified) of every PLZ."""
    cov.ensure(plz_list, wait_s=wait_s)
    flags = read_flags()
    statuses = cov.statuses(plz_list, flags)
    generated = generated_plz(version_id, plz_list)
    states = {}
    for plz in plz_list:
        st = statuses[plz]
        states[plz] = ("exists" if plz in generated else "blocked" if not st["selectable"]
                       else "unverified" if not st["verified"] else "new")
    return {"flags": flags, "statuses": statuses, "states": states, "generated": generated}


def check_payload(plz_list: list[int], cov: InputCoverage, version_id: str) -> dict[str, Any]:
    """The gate part of ``GET /api/jobs/generate/check``."""
    c = classify(plz_list, cov, version_id, wait_s=3)
    regions = {}
    for plz in plz_list:
        st = c["statuses"][plz]
        counts = st.get("counts") or {}
        regions[plz] = {"state": c["states"][plz], "buildings": counts.get("buildings_importable"),
                        "buildings_total": counts.get("buildings_total"),
                        "street_segments": counts.get("street_segments"),
                        "connection_lines": counts.get("connection_lines"), "input": st}
    blocked = [p for p in plz_list if c["states"][p] == "blocked"]
    unverified = [p for p in plz_list if c["states"][p] == "unverified"]
    selection = set(plz_list)
    ags = []
    municipalities = register_by_ags(plz_list)
    others = sorted({p for m in municipalities.values() for p in m["plz"]} - selection)
    other_statuses = cov.statuses(others, c["flags"], detail=False) if others else {}
    for code, m in municipalities.items():
        def state_of(p: int) -> str:
            if p in selection:
                return c["states"][p]
            s = other_statuses.get(p) or {}
            return "blocked" if not s.get("selectable") else "unverified" if not s.get("verified") else "new"
        states = {p: state_of(p) for p in m["plz"]}
        ags.append({"ags": str(code).zfill(8), "name_city": m["name_city"], "plz": m["plz"],
                    "selectable": [p for p, s in states.items() if s in ("new", "exists", "unverified")],
                    "blocked": [p for p, s in states.items() if s == "blocked"],
                    "unverified": [p for p, s in states.items() if s == "unverified"],
                    "outside_selection": [p for p in m["plz"] if p not in selection]})
    info = cov.brief_status()
    return {"regions": regions, "blocked": blocked, "unverified": unverified, "input_verified": not unverified,
            "coverage": {k: info[k] for k in ("mode", "state", "version", "flags_key", "building", "stale", "error")},
            "filters": c["flags"], "ags": ags}


def gate_generate(plz_list: list[int], ags_list: list[int] | None, cov: InputCoverage, version_id: str, *,
                  skip_blocked: bool, include_blocked: bool, allow_unverified: bool) -> dict[str, Any]:
    """Apply the gate to a generate request.

    Returns:
        ``{"plz": kept, "use_ags": bool, "skipped": [...], "included_blocked": [...],
        "unverified": [...], "notes": [...]}``.

    Raises:
        HTTPException: 400 for contradicting options, 409 when blocked or unverified PLZ need an
            explicit decision, 422 when nothing is left to generate.
    """
    if skip_blocked and include_blocked:
        raise HTTPException(400, "skip_blocked and include_blocked exclude each other")
    c = classify(plz_list, cov, version_id, wait_s=15)
    statuses, states = c["statuses"], c["states"]
    blocked = [p for p in plz_list if states[p] == "blocked"]
    unverified = [p for p in plz_list if states[p] == "unverified"]
    detail = [dict(brief(statuses[p]), plz=p, reason=statuses[p]["reason"]) for p in blocked]
    notes: list[str] = []
    kept = list(plz_list)
    skipped: list[int] = []
    included: list[int] = []
    if blocked:
        if skip_blocked:
            kept = [p for p in kept if p not in blocked]
            skipped = blocked
            notes.append("pylovo-api: skipped PLZ without input data: "
                         + ", ".join(f"{p} ({statuses[p]['short']})" for p in blocked))
        elif include_blocked:
            included = blocked
            notes.append("pylovo-api: included without input data: "
                         + ", ".join(f"{p} ({statuses[p]['short']})" for p in blocked))
        else:
            raise HTTPException(409, {"message": f"{len(blocked)} PLZ without input data for pylovo-generate: "
                                      + ", ".join(f"{d['plz']} ({d['short']})" for d in detail), "blocked": detail})
    if unverified:
        if not allow_unverified:
            progress = cov.brief_status().get("progress") or {}
            raise HTTPException(409, {"message": f"The input data of {len(unverified)} PLZ is still being checked.",
                                      "unverified": unverified, "eta_s": progress.get("eta_s")})
        notes.append("pylovo-api: started before the input check finished for: " + ", ".join(map(str, unverified)))
    if not kept:
        raise HTTPException(422, {"message": "No PLZ with input data left to generate.", "blocked": detail})
    use_ags = bool(ags_list) and not skipped and not unverified and not included
    return {"plz": kept, "use_ags": use_ags, "skipped": skipped, "included_blocked": included,
            "unverified": unverified, "notes": notes}
