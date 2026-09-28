"""Jobs: start ``pylovo-*`` commands, follow their logs (SSE), cancel, download outputs."""
from __future__ import annotations

import asyncio
import json
import mimetypes
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from pylovo_api import config_io, db, gate, queries
from pylovo_api.deps import config_version_id, coverage, jobs, require_confirm
from pylovo_api.jobs import JobConflict, pylovo_command
from pylovo_api.routers.transformers import upload_path
from pylovo_api.settings import paths

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class GenerateBody(BaseModel):
    plz: list[int] | None = None
    ags: list[int] | None = None
    parallel: bool = True
    # region gate (pylovo_api.gate): what to do with PLZ without input data / not checked yet
    skip_blocked: bool = False
    include_blocked: bool = False
    allow_unverified: bool = False


class PlzBody(BaseModel):
    plz: int


class ResetBody(BaseModel):
    confirm: str | None = None


class DeleteVersionsBody(BaseModel):
    version_ids: list[str] = Field(min_length=1)
    confirm: str | None = None


class DeleteNetworksBody(BaseModel):
    plz: int
    version_id: str
    confirm: str | None = None


class ExportBody(BaseModel):
    plz: list[int] = Field(min_length=1)
    kcid: int | None = None
    bcid: int | None = None


class OsmImportBody(BaseModel):
    relation_id: int = Field(ge=1)


class DsoImportBody(BaseModel):
    upload_id: str
    source: str | None = Field(None, max_length=60)
    replace_source: bool = False
    confirm: str | None = None


def _start(request: Request, kind: str, title: str, argv: list[str], writes_db: bool = True,
           params: dict | None = None, output_dir: Path | None = None) -> dict:
    try:
        job = jobs(request).start(kind, title, argv, writes_db=writes_db, params=params, output_dir=output_dir)
    except JobConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return job.summary()


def _resolve_plz(plz: list[int] | None, ags: list[int] | None) -> list[int]:
    if bool(plz) == bool(ags):
        raise HTTPException(400, "Give either PLZ or AGS codes")
    if plz:
        return sorted(set(plz))
    rows = db.fetch_all("SELECT DISTINCT plz FROM pylovo.municipal_register WHERE ags = ANY(%s) ORDER BY plz", (ags,))
    if not rows:
        raise HTTPException(404, "No PLZ found for these AGS in the municipal register")
    return [r["plz"] for r in rows]


# --------------------------------------------------------------------------- start jobs
@router.get("/generate/check")
def generate_check(request: Request, plz: Annotated[list[int] | None, Query()] = None,
                   ags: Annotated[list[int] | None, Query()] = None) -> dict:
    """What ``pylovo-generate`` will do for these regions with the current config."""
    plz_list = _resolve_plz(plz, ags)
    values = config_io.current_values()
    version_id = str(values.get("VERSION_ID", ""))
    with db.cursor() as cur:
        cur.execute("SELECT plz FROM pylovo.postcode WHERE plz = ANY(%s)", (plz_list,))
        available = {r["plz"] for r in cur.fetchall()}
        cur.execute("SELECT postcode_result_plz AS plz FROM pylovo.postcode_result WHERE version_id = %s "
                    "AND postcode_result_plz = ANY(%s)", (version_id, plz_list))
        generated = {r["plz"] for r in cur.fetchall()}
        cur.execute(f"""SELECT p.plz, {queries.TRAFO_SOURCE_SQL} AS source, count(*) AS n
                        FROM pylovo.transformers t JOIN pylovo.postcode p ON ST_Intersects(t.geom, p.geom)
                        WHERE p.plz = ANY(%(plz)s) GROUP BY 1, 2""", {"plz": plz_list})
        trafos: dict[int, dict[str, int]] = {}
        for r in cur.fetchall():
            trafos.setdefault(r["plz"], {})[r["source"]] = r["n"]
    # region gate: state new | exists | blocked | unverified and the importable input counts
    gated = gate.check_payload(plz_list, coverage(request), version_id)
    regions = []
    for p in plz_list:
        regions.append({"plz": p, "has_geometry": p in available, "exists": p in generated,
                        "transformers": trafos.get(p, {}), **gated["regions"][p]})
    return {
        "version_id": version_id, "version_comment": values.get("VERSION_COMMENT"),
        "version": queries.version_exists(version_id),
        "use_open": values.get("USE_OPEN_TRANSFORMER_POSITIONS"), "use_dso": values.get("USE_DSO_TRANSFORMER_POSITIONS"),
        "use_manual": values.get("USE_MANUAL_TRANSFORMER_POSITIONS"),
        "analyze": values.get("ANALYZE_GRIDS"), "n_jobs_percent": values.get("N_JOBS_PERCENT"),
        "regions": regions, **{k: v for k, v in gated.items() if k != "regions"},
    }


@router.post("/generate", status_code=202)
def generate(body: GenerateBody, request: Request) -> dict:
    """``pylovo-generate --plz ... | --ags ... --parallel|--no-parallel``, gated by the region gate.

    PLZ without input data are refused (409) unless ``skip_blocked`` drops them or
    ``include_blocked`` keeps them; PLZ whose check has not finished need ``allow_unverified``.
    ``--ags`` is passed on only when every PLZ of the municipality passes the gate.
    """
    if bool(body.plz) == bool(body.ags):
        raise HTTPException(400, "Give either PLZ or AGS codes")
    resolved = _resolve_plz(body.plz, body.ags)
    decision = gate.gate_generate(resolved, body.ags, coverage(request), config_version_id(),
                                  skip_blocked=body.skip_blocked, include_blocked=body.include_blocked,
                                  allow_unverified=body.allow_unverified)
    args = (["--ags", *(str(a).zfill(8) for a in body.ags)] if decision["use_ags"]
            else ["--plz", *map(str, decision["plz"])])
    args.append("--parallel" if body.parallel else "--no-parallel")   # explicit: PARALLEL in the config is only the CLI default
    codes = body.ags if decision["use_ags"] else decision["plz"]
    label = ", ".join(map(str, codes[:4])) + (" …" if len(codes) > 4 else "")
    return _start(request, "generate", f"Generate grids · {label} · v{config_version_id()}",
                  pylovo_command("pylovo-generate", *args),
                  params={"plz": decision["plz"], "ags": body.ags if decision["use_ags"] else None,
                          "parallel": body.parallel, "version_id": config_version_id(),
                          "skipped": decision["skipped"], "included_blocked": decision["included_blocked"],
                          "unverified": decision["unverified"], "notes": decision["notes"]})


@router.post("/analyze", status_code=202)
def analyze(body: PlzBody, request: Request) -> dict:
    """``pylovo-analyze --plz <plz> --all`` for the config ``VERSION_ID``."""
    version_id = config_version_id()
    return _start(request, "analyze", f"Analyse grids · {body.plz} · v{version_id}",
                  pylovo_command("pylovo-analyze", "--plz", str(body.plz), "--all"),
                  params={"plz": body.plz, "version_id": version_id})


@router.post("/setup", status_code=202)
def setup(request: Request) -> dict:
    """``pylovo-setup``: create a missing schema or apply pending migrations; keeps all grids."""
    dbname = db.settings()["dbname"]
    return _start(request, "setup", f"Database setup · {dbname}", pylovo_command("pylovo-setup"),
                  params={"dbname": dbname})


@router.post("/reset", status_code=202)
def reset(body: ResetBody, request: Request) -> dict:
    """``pylovo-setup reset``: drops and rebuilds the ``pylovo`` schema. Type the database name."""
    dbname = db.settings()["dbname"]
    require_confirm(body.confirm, dbname, "drop and rebuild the pylovo schema")
    return _start(request, "reset", f"Database reset · {dbname}",
                  pylovo_command("pylovo-setup", "reset", "--database", dbname, "--yes"), params={"dbname": dbname})


@router.post("/delete-versions", status_code=202)
def delete_versions(body: DeleteVersionsBody, request: Request) -> dict:
    """``pylovo-delete --version <ids>``: removes all results of these versions."""
    ids = [v.strip() for v in body.version_ids if v.strip()]
    require_confirm(body.confirm, " ".join(ids), "delete these versions")
    return _start(request, "delete", f"Delete version {' '.join(ids)}",
                  pylovo_command("pylovo-delete", "--version", *ids), params={"version_ids": ids})


@router.post("/delete-networks", status_code=202)
def delete_networks(body: DeleteNetworksBody, request: Request) -> dict:
    """``pylovo-delete networks --plz <plz> --version <id>``"""
    require_confirm(body.confirm, f"{body.version_id}/{body.plz}", "delete the grids of this PLZ")
    return _start(request, "delete", f"Delete grids · {body.plz} · v{body.version_id}",
                  pylovo_command("pylovo-delete", "networks", "--plz", str(body.plz), "--version", body.version_id),
                  params={"plz": body.plz, "version_id": body.version_id})


@router.post("/export", status_code=202)
def export(body: ExportBody, request: Request) -> dict:
    """``pylovo-export`` into a private directory; the CSVs are offered for download afterwards."""
    args = ["--plz", *map(str, body.plz)]
    if body.kcid is not None or body.bcid is not None:
        if body.kcid is None or body.bcid is None or len(body.plz) != 1:
            raise HTTPException(400, "A grid export needs exactly one PLZ, kcid and bcid")
        args = ["--grid", "--plz", str(body.plz[0]), "--kcid", str(body.kcid), "--bcid", str(body.bcid)]
    out = paths().exports_dir / uuid.uuid4().hex[:10]
    return _start(request, "export", f"Export geodata · {', '.join(map(str, body.plz))} · v{config_version_id()}",
                  pylovo_command("pylovo-export", *args, "--output", str(out)), writes_db=False,
                  params=body.model_dump() | {"version_id": config_version_id()}, output_dir=out)


@router.post("/import-osm", status_code=202)
def import_osm(body: OsmImportBody, request: Request) -> dict:
    """``pylovo-import transformers-osm --relation-id <id>`` (queries the Overpass API)."""
    return _start(request, "import", f"OSM transformers · relation {body.relation_id}",
                  pylovo_command("pylovo-import", "transformers-osm", "--relation-id", str(body.relation_id)),
                  params=body.model_dump())


@router.post("/import-dso", status_code=202)
def import_dso(body: DsoImportBody, request: Request) -> dict:
    """``pylovo-import transformers-dso-csv <file> [--source S] [--replace-source]``"""
    path = upload_path(body.upload_id)
    args = ["transformers-dso-csv", str(path)]
    if body.source:
        args += ["--source", body.source]
    if body.replace_source:
        if not body.source:
            raise HTTPException(400, "Replacing a source requires the source name")
        require_confirm(body.confirm, body.source, "delete all existing transformers of this DSO source")
        args.append("--replace-source")
    return _start(request, "import", f"DSO transformers · {body.source or 'csv'}",
                  pylovo_command("pylovo-import", *args), params=body.model_dump(exclude={"confirm"}))


# --------------------------------------------------------------------------- observe jobs
def _job(request: Request, job_id: str):
    job = jobs(request).get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@router.get("")
def list_jobs(request: Request) -> list[dict]:
    """All jobs of this server session (and recent history), newest first."""
    return [j.summary() for j in jobs(request).list()]


@router.get("/{job_id}")
def get_job(job_id: str, request: Request, tail: int = Query(0, ge=0, le=5000)) -> dict:
    """One job (optionally with the last ``tail`` log lines)."""
    job = _job(request, job_id)
    data = job.summary()
    if tail:
        data["lines"] = job.lines[-tail:]
    return data


@router.get("/{job_id}/log")
def job_log(job_id: str, request: Request, after: int = 0, format: str = "json"):
    """Log lines after a sequence number (``format=text`` downloads the whole log)."""
    job = _job(request, job_id)
    if format == "text":
        text = "\n".join(line["text"] for line in job.lines) + "\n"
        return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="pylovo-job-{job.id}.log"'})
    return {"job": job.summary(), "lines": job.lines_after(after)}


@router.get("/{job_id}/events")
async def job_events(job_id: str, request: Request, after: int = 0) -> StreamingResponse:
    """Server-Sent Events: ``log`` events with new lines, ``status`` updates and a final ``end``."""
    job = _job(request, job_id)
    last_id = request.headers.get("last-event-id")
    cursor = int(last_id) + 1 if last_id and last_id.isdigit() else after

    async def stream():
        try:
            async for chunk in _events():
                yield chunk
        except asyncio.CancelledError:  # server shutdown or client gone
            return

    async def _events():
        nonlocal cursor
        last_status = None
        idle = 0.0
        yield "retry: 2000\n\n"
        server = getattr(request.app.state, "server", None)
        while True:
            if await request.is_disconnected() or (server is not None and server.should_exit):
                return
            lines = job.lines_after(cursor)
            if lines:
                cursor = lines[-1]["seq"] + 1
                for start in range(0, len(lines), 400):
                    chunk = lines[start:start + 400]
                    yield f"id: {chunk[-1]['seq']}\nevent: log\ndata: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                idle = 0.0
            summary = job.summary()
            status_key = (summary["status"], summary["progress"], summary["phase"], summary["counts"]["error"],
                          summary["counts"]["warning"])
            if status_key != last_status:
                last_status = status_key
                yield f"event: status\ndata: {json.dumps(summary)}\n\n"
            if not job.active and not job.lines_after(cursor):
                yield f"event: end\ndata: {json.dumps(summary)}\n\n"
                return
            await asyncio.sleep(0.25)
            idle += 0.25
            if idle >= 15:
                idle = 0.0
                yield ": keep-alive\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str, request: Request) -> dict:
    """Stop a running job (SIGINT to its process group, then SIGTERM/SIGKILL)."""
    job = _job(request, job_id)
    if not job.active:
        raise HTTPException(409, f"Job is already {job.status}")
    jobs(request).cancel(job_id)
    return job.summary()


@router.get("/{job_id}/files")
def job_files(job_id: str, request: Request) -> list[dict]:
    """Files written by an export job."""
    job = _job(request, job_id)
    if not job.output_dir or not Path(job.output_dir).is_dir():
        return []
    root = Path(job.output_dir)
    return [{"name": p.name, "size": p.stat().st_size} for p in sorted(root.iterdir()) if p.is_file()]


@router.get("/{job_id}/files/{name}")
def job_file(job_id: str, name: str, request: Request) -> FileResponse:
    """Download one file written by an export job."""
    job = _job(request, job_id)
    if not job.output_dir:
        raise HTTPException(404, "This job has no output files")
    root = Path(job.output_dir).resolve()
    path = (root / name).resolve()
    if path.parent != root or not path.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(path, filename=name, media_type=mimetypes.guess_type(name)[0] or "application/octet-stream")
