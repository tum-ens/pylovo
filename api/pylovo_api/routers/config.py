"""``config_generation.yaml``: form values, raw editor, validation, backups."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from pylovo_api import config_io, queries

router = APIRouter(prefix="/api/config", tags=["config"])


class TextBody(BaseModel):
    text: str
    base_sha: str | None = None
    confirm: bool = False
    deep: bool = True


class ValuesBody(BaseModel):
    changes: dict[str, Any]
    base_sha: str | None = None
    confirm: bool = False
    dry_run: bool = False


class RestoreBody(BaseModel):
    name: str
    confirm: bool = False


def _state() -> dict[str, Any]:
    snap = config_io.read_config()
    version_id = str(snap.values.get("VERSION_ID", ""))
    return {
        "path": str(config_io.paths().config_file),
        "text": snap.text,
        "sha": snap.sha,
        "mtime": snap.mtime,
        "values": {k: snap.values.get(k) for k in config_io.FORM_FIELDS},
        "sections": config_io.form_schema(snap.text),
        "backups": config_io.list_backups()[:20],
        "version": queries.version_exists(version_id) if version_id else {"exists": False},
    }


@router.get("")
def get_config() -> dict:
    """Current file text, its hash, the form values with help texts and recent backups."""
    return _state()


@router.post("/validate")
def validate(body: TextBody) -> dict:
    """Validate a candidate text (YAML syntax, field types, ``pylovo.config_loader`` import)."""
    current = config_io.read_config()
    _, issues = config_io.validate_text(body.text, deep=body.deep)
    return {"ok": not any(i["level"] == "error" for i in issues), "issues": issues,
            "diff": config_io.diff(current.text, body.text), "changed": body.text != current.text}


@router.put("")
def save(body: TextBody) -> dict:
    """Overwrite the file with a validated text; the old file is kept as a backup."""
    if not body.confirm:
        raise HTTPException(400, "Saving overwrites config_generation.yaml; send confirm=true after reviewing the diff.")
    try:
        result = config_io.save_text(body.text, body.base_sha)
    except config_io.ConfigError as exc:
        status = 409 if any(i.get("conflict") for i in exc.issues) else 422
        raise HTTPException(status, {"message": str(exc), "issues": exc.issues}) from exc
    return result | {"state": _state()}


@router.post("/values")
def set_values(body: ValuesBody) -> dict:
    """Apply form edits to single keys (comments stay intact). ``dry_run`` returns the diff only."""
    unknown = [k for k in body.changes if k not in config_io.FORM_FIELDS]
    if unknown:
        raise HTTPException(400, f"Not editable in the form: {', '.join(unknown)} (use the YAML editor)")
    current = config_io.read_config()
    try:
        text = config_io.set_top_level_values(current.text, body.changes)
    except config_io.ConfigError as exc:
        raise HTTPException(422, {"message": str(exc), "issues": exc.issues}) from exc
    if body.dry_run:
        _, issues = config_io.validate_text(text, deep=False)
        return {"text": text, "diff": config_io.diff(current.text, text), "issues": issues}
    return save(TextBody(text=text, base_sha=body.base_sha, confirm=body.confirm))


@router.get("/backups/{name}", response_class=PlainTextResponse)
def backup(name: str) -> str:
    """Text of one backup."""
    try:
        return config_io.backup_path(name).read_text(encoding="utf-8")
    except config_io.ConfigError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/restore")
def restore(body: RestoreBody) -> dict:
    """Restore a backup (the current file is backed up first)."""
    try:
        text = config_io.backup_path(body.name).read_text(encoding="utf-8")
    except config_io.ConfigError as exc:
        raise HTTPException(404, str(exc)) from exc
    return save(TextBody(text=text, confirm=body.confirm))
