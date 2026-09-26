"""Reading, validating and saving ``config/config_generation.yaml``.

``pylovo.config_loader`` freezes all values when it is imported, so the UI never uses it for
display: every request reads the YAML file fresh. Saving always

1. validates the YAML syntax and a few types the form knows about,
2. imports ``pylovo.config_loader`` in a *subprocess* against a temporary copy of ``config/``
   (this catches every error pylovo itself would raise, e.g. a missing key), and
3. copies the current file to ``.pylovo-api/config-backups/`` before it is overwritten.

Form edits change single top-level keys in place, so comments and layout of the file survive.
"""
from __future__ import annotations

import contextlib
import difflib
import hashlib
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from pylovo_api.settings import paths

MAX_BACKUPS = 50

# The key generation parameters shown in the form. Help texts come from the YAML comments.
FORM_SECTIONS: list[dict[str, Any]] = [
    {"id": "version", "title": "Version", "fields": [
        {"key": "VERSION_ID", "type": "string", "maxLength": 10, "label": "Version id"},
        {"key": "VERSION_COMMENT", "type": "string", "label": "Version comment"},
        {"key": "N_JOBS_PERCENT", "type": "int", "min": 1, "max": 100, "unit": "% of cores", "label": "Parallel workers"},
        {"key": "ANALYZE_GRIDS", "type": "bool", "label": "Analyse grids after generation"},
        {"key": "LOG_LEVEL", "type": "enum", "options": ["DEBUG", "INFO", "WARNING", "ERROR"], "label": "Log level"},
    ]},
    {"id": "consumers", "title": "Consumers & loads", "fields": [
        {"key": "RESIDENTIAL_ONLY_GENERATION", "type": "bool", "label": "Residential buildings only"},
        {"key": "EXCLUDE_BUILDINGS_WITHOUT_ADDRESS", "type": "bool", "label": "Exclude buildings without address"},
        {"key": "PEAK_LOAD_HOUSEHOLD", "type": "float", "min": 0, "unit": "kW", "label": "Peak load per household"},
        {"key": "MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW", "type": "float", "min": 0, "unit": "kW", "label": "MV direct connection above"},
    ]},
    {"id": "cables", "title": "Cable dimensioning", "fields": [
        {"key": "MAX_END_TO_END_FEEDER_VOLTAGE_DROP_PERCENT", "type": "float", "min": 0, "max": 20, "unit": "%", "label": "Max. feeder voltage drop"},
        {"key": "MAX_SERVICE_DESIGN_VOLTAGE_DROP_PERCENT", "type": "float", "min": 0, "max": 20, "unit": "%", "label": "Max. service voltage drop"},
        {"key": "FEEDER_SPLIT_MAX_CURRENT_KA", "type": "float", "min": 0, "unit": "kA", "label": "Feeder split current"},
        {"key": "MIN_SHARED_PREFIX_LENGTH_M", "type": "float", "min": 0, "unit": "m", "label": "Min. shared prefix length"},
        {"key": "AGGREGATE_NEARBY_CONNECTION_POINTS", "type": "bool", "label": "Aggregate nearby connection points"},
        {"key": "CONNECTION_POINT_AGGREGATION_RADIUS_M", "type": "float", "min": 0, "unit": "m", "label": "Aggregation radius"},
        {"key": "CONNECTION_POINT_AGGREGATION_MAX_BUILDINGS", "type": "int", "min": 1, "label": "Aggregation max. buildings"},
    ]},
    {"id": "settlement", "title": "Settlement type", "fields": [
        {"key": "RURAL_MAX_HOUSEHOLDS", "type": "float", "min": 0, "unit": "hh/building", "label": "Rural: max. households"},
        {"key": "URBAN_MIN_HOUSEHOLDS", "type": "float", "min": 0, "unit": "hh/building", "label": "Urban: min. households"},
        {"key": "RURAL_MIN_BUILDING_DISTANCE", "type": "float", "min": 0, "unit": "m", "label": "Rural: min. building distance"},
        {"key": "URBAN_MAX_BUILDING_DISTANCE", "type": "float", "min": 0, "unit": "m", "label": "Urban: max. building distance"},
        {"key": "TRANSFORMER_MAPPING", "type": "mapping", "label": "Transformer sizes per settlement type"},
    ]},
    {"id": "clustering", "title": "Clustering & transformer placement", "fields": [
        {"key": "MAX_BUILDINGS_PER_KCID", "type": "int", "min": 1, "label": "Max. buildings per k-means cluster"},
        {"key": "K_MEANS_SEED", "type": "int", "min": 0, "label": "k-means seed"},
        {"key": "MAX_GREENFIELD_TRAFO_DISTANCE", "type": "float", "min": 0, "unit": "m", "label": "Greenfield: max. distance"},
        {"key": "MAX_GREENFIELD_TRAFO_DISTANCE_STD", "type": "float", "min": 0, "unit": "m", "label": "Greenfield: distance spread"},
        {"key": "GREENFIELD_TRAFO_POSITION_TOLERANCE", "type": "float", "min": 0, "label": "Greenfield: position tolerance"},
        {"key": "TRANSFORMER_PLANNING_UTILIZATION", "type": "float", "min": 0.1, "max": 1.5, "label": "Planning utilisation"},
        {"key": "MERGE_GREENFIELD_CLUSTERS", "type": "bool", "label": "Merge greenfield clusters"},
        {"key": "GREENFIELD_CLUSTER_MERGE_TRANSFORMER_KVA", "type": "kva_list", "unit": "kVA", "label": "Merge transformer sizes"},
        {"key": "MAX_BROWNFIELD_TRAFO_DISTANCE", "type": "float", "min": 0, "unit": "m", "label": "Brownfield: max. distance"},
        {"key": "USE_OPEN_TRANSFORMER_POSITIONS", "type": "bool", "label": "Use OSM / LoD2 / manual transformer positions"},
        {"key": "USE_DSO_TRANSFORMER_POSITIONS", "type": "bool", "label": "Use DSO transformer positions"},
        {"key": "USE_MANUAL_TRANSFORMER_POSITIONS", "type": "bool", "label": "Use manual (UI) positions only"},
    ]},
]
FORM_FIELDS = {f["key"]: f for section in FORM_SECTIONS for f in section["fields"]}
_KEY_LINE_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*):(?P<rest>.*)$")


class ConfigError(ValueError):
    """A configuration text that cannot be saved; ``issues`` lists the reasons."""

    def __init__(self, message: str, issues: list[dict] | None = None):
        super().__init__(message)
        self.issues = issues or [{"level": "error", "message": message}]


@dataclass
class ConfigSnapshot:
    text: str
    sha: str
    mtime: float
    values: dict[str, Any]


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_config() -> ConfigSnapshot:
    """Read ``config_generation.yaml`` fresh from disk."""
    path = paths().config_file
    text = path.read_text(encoding="utf-8")
    try:
        values = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        values = {}
    return ConfigSnapshot(text=text, sha=sha(text), mtime=path.stat().st_mtime, values=values)


def current_values() -> dict[str, Any]:
    """Parsed values of the current config (empty dict if the file is broken)."""
    try:
        return read_config().values
    except OSError:
        return {}


def _split_comment(rest: str) -> tuple[str, str]:
    """Split ``' value # comment'`` into value and comment (quotes are respected)."""
    quote = None
    for i, ch in enumerate(rest):
        if ch in "\"'" and quote is None:
            quote = ch
        elif ch == quote:
            quote = None
        elif ch == "#" and quote is None and (i == 0 or rest[i - 1] in " \t"):
            return rest[:i].rstrip(), rest[i + 1:].strip()
    return rest.rstrip(), ""


def comments_by_key(text: str) -> dict[str, str]:
    """Help text for every top-level key: the comment lines above it plus its inline comment."""
    help_text: dict[str, str] = {}
    pending: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            body = stripped.lstrip("#").strip()
            if body and not set(body) <= set("=-") and not body.isupper():
                pending.append(body)
            elif not body or set(body) <= set("=-"):
                pending = []
            continue
        match = _KEY_LINE_RE.match(line)
        if match:
            _, inline = _split_comment(match.group("rest"))
            parts = [p for p in pending if not p.startswith("- {")] + ([inline] if inline else [])
            help_text[match.group("key")] = " ".join(parts).strip()
            pending = []
        elif not stripped:
            pending = []
    return help_text


def form_schema(text: str) -> list[dict[str, Any]]:
    """The form sections with the YAML comment of each field as help text."""
    help_text = comments_by_key(text)
    sections = []
    for section in FORM_SECTIONS:
        fields = [dict(f, help=help_text.get(f["key"], "")) for f in section["fields"]]
        sections.append(dict(section, fields=fields))
    return sections


# --------------------------------------------------------------------------- editing
def _render_scalar(value: Any, key: str) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return repr(value)
    if value is None:
        return "null"
    text = str(value)
    if key == "VERSION_ID" or not re.fullmatch(r"[A-Za-z_][\w .,/()+-]*", text) or text.lower() in (
            "true", "false", "yes", "no", "null", "on", "off"):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def _render_flow(value: Any, key: str) -> str:
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_render_flow(v, key) for v in value) + "]"
    return _render_scalar(value, key)


_MISSING = object()


def _same_value(old: Any, new: Any) -> bool:
    if old is _MISSING:
        return False
    if isinstance(old, dict) and isinstance(new, dict):
        return {str(k): v for k, v in old.items()} == {str(k): v for k, v in new.items()}
    return type(old) is type(new) and old == new or (
        isinstance(old, (int, float)) and isinstance(new, (int, float))
        and not isinstance(old, bool) and not isinstance(new, bool) and float(old) == float(new))


def set_top_level_values(text: str, changes: dict[str, Any]) -> str:
    """Return ``text`` with the given top-level keys replaced in place.

    Scalars and flat lists are written on the key line (keeping an inline comment); mappings
    such as ``TRANSFORMER_MAPPING`` replace the indented block below the key. Keys that are
    not in the file are appended at the end.
    """
    lines = text.splitlines(keepends=True)
    try:
        before = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        before = {}
    changes = {k: v for k, v in changes.items() if not _same_value(before.get(k, _MISSING), v)}
    for key, value in changes.items():
        index = next((i for i, line in enumerate(lines) if line.startswith(f"{key}:")), None)
        if index is None:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            index = len(lines)
            lines.append(f"{key}: null\n")
        match = _KEY_LINE_RE.match(lines[index].rstrip("\r\n"))
        rest = match.group("rest") if match else ""
        old_value, comment = _split_comment(rest)
        gap = re.match(r"\s*", rest[len(old_value):]).group(0) or " " if comment else ""
        suffix = f"{gap}#{rest.split('#', 1)[1] if '#' in rest[len(old_value):] else ' ' + comment}" if comment else ""
        end = index + 1
        while end < len(lines) and lines[end].strip() and (
                lines[end][:1] in (" ", "\t") or (not old_value.strip() and lines[end].startswith("-"))):
            end += 1
        if isinstance(value, dict):
            block = [f"{key}:{suffix}\n"] + [f"  {k}: {_render_flow(v, key)}\n" for k, v in value.items()]
        else:
            block = [f"{key}: {_render_flow(value, key)}{suffix}\n"]
        lines[index:end] = block
    new_text = "".join(lines)
    parsed = yaml.safe_load(new_text) or {}
    for key, value in changes.items():
        got = parsed.get(key)
        if isinstance(value, dict):
            got = {str(k): v for k, v in (got or {}).items()}
            value = {str(k): v for k, v in value.items()}
        if got != value:
            raise ConfigError(f"Could not write {key} safely (wrote {got!r}, expected {value!r}).")
    return new_text


# --------------------------------------------------------------------------- validation
def _field_issues(values: dict[str, Any]) -> list[dict]:
    issues = []
    for key, field in FORM_FIELDS.items():
        if key not in values:
            continue
        value = values[key]
        kind = field["type"]
        bad = False
        if kind == "bool":
            bad = not isinstance(value, bool)
        elif kind in ("int", "float"):
            bad = isinstance(value, bool) or not isinstance(value, (int, float)) or (kind == "int" and not isinstance(value, int))
            if not bad and "min" in field and value < field["min"]:
                issues.append({"level": "error", "key": key, "message": f"{key} must be ≥ {field['min']}"})
            if not bad and "max" in field and value > field["max"]:
                issues.append({"level": "error", "key": key, "message": f"{key} must be ≤ {field['max']}"})
        elif kind == "string":
            bad = not isinstance(value, str)
            if not bad and field.get("maxLength") and len(value) > field["maxLength"]:
                issues.append({"level": "error", "key": key,
                               "message": f"{key} may have at most {field['maxLength']} characters (database column)"})
        elif kind == "enum":
            bad = value not in field["options"]
        elif kind == "kva_list":
            bad = not isinstance(value, list) or not all(isinstance(v, (int, float)) for v in value)
        elif kind == "mapping":
            bad = not isinstance(value, dict) or not all(isinstance(v, list) for v in value.values())
        if bad:
            issues.append({"level": "error", "key": key, "message": f"{key} has an invalid value {value!r} (expected {kind})"})
    kva = {t.get("s_max_kva") for t in values.get("TRANSFORMERS") or [] if isinstance(t, dict)}
    if kva:
        mapping = values.get("TRANSFORMER_MAPPING") or {}
        if isinstance(mapping, dict):
            for settlement, sizes in mapping.items():
                missing = [s for s in (sizes or []) if s not in kva]
                if missing:
                    issues.append({"level": "warning", "key": "TRANSFORMER_MAPPING",
                                   "message": f"Settlement type {settlement} uses sizes without a TRANSFORMERS entry: {missing}"})
        merge = values.get("GREENFIELD_CLUSTER_MERGE_TRANSFORMER_KVA") or []
        if isinstance(merge, list) and [s for s in merge if s not in kva]:
            issues.append({"level": "warning", "key": "GREENFIELD_CLUSTER_MERGE_TRANSFORMER_KVA",
                           "message": "Some merge sizes have no TRANSFORMERS entry"})
    return issues


def _pylovo_import_check(text: str) -> list[dict]:
    """Import ``pylovo.config_loader`` in a subprocess against a temporary ``config/`` copy."""
    # The subprocess finds no .env next to the temporary directory; importing the loader here
    # first puts the database settings of this project into os.environ, which it inherits.
    with contextlib.suppress(Exception):  # the saved file may be the broken one being repaired
        import pylovo.config_loader  # noqa: F401

    work = paths().tmp_dir / f"validate-{uuid.uuid4().hex[:8]}"
    config_dir = work / "config"
    try:
        shutil.copytree(paths().config_dir, config_dir, ignore=shutil.ignore_patterns("*.bak", "backups"))
        (config_dir / "config_generation.yaml").write_text(text, encoding="utf-8")
        probe = "import pylovo.config_loader as c; print('pylovo accepted VERSION_ID', repr(c.VERSION_ID))"
        proc = subprocess.run([sys.executable, "-c", probe], cwd=work, capture_output=True, text=True, timeout=120,
                              check=False)
        if proc.returncode != 0:
            lines = [ln for ln in proc.stderr.strip().splitlines() if ln.strip()]
            return [{"level": "error", "message": "pylovo rejects this configuration: " + (lines[-1] if lines else "unknown error"),
                     "detail": "\n".join(lines[-12:])}]
        return []
    except subprocess.TimeoutExpired:
        return [{"level": "warning", "message": "The pylovo import check timed out; the YAML syntax is valid."}]
    finally:
        shutil.rmtree(work, ignore_errors=True)


def validate_text(text: str, deep: bool = True) -> tuple[dict[str, Any], list[dict]]:
    """Validate a candidate configuration.

    Returns:
        The parsed values and a list of issues (``level`` is ``error`` or ``warning``).
    """
    try:
        values = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        issue = {"level": "error", "message": f"YAML syntax error: {getattr(exc, 'problem', None) or exc}"}
        if mark is not None:
            issue.update(line=mark.line + 1, column=mark.column + 1)
        return {}, [issue]
    if not isinstance(values, dict):
        return {}, [{"level": "error", "message": "The configuration must be a YAML mapping (KEY: value lines)."}]
    issues = _field_issues(values)
    if deep and not any(i["level"] == "error" for i in issues):
        issues += _pylovo_import_check(text)
    return values, issues


def diff(old: str, new: str) -> str:
    """Unified diff between the saved and the candidate configuration."""
    return "".join(difflib.unified_diff([ln + "\n" for ln in old.splitlines()], [ln + "\n" for ln in new.splitlines()],
                                        "config_generation.yaml (saved)", "config_generation.yaml (new)", n=2))


# --------------------------------------------------------------------------- saving
def list_backups() -> list[dict[str, Any]]:
    items = []
    for path in sorted(paths().backups_dir.glob("config_generation.*.yaml"), reverse=True):
        items.append({"name": path.name, "size": path.stat().st_size, "mtime": path.stat().st_mtime})
    return items


def backup_path(name: str) -> Path:
    path = (paths().backups_dir / name).resolve()
    if path.parent != paths().backups_dir.resolve() or not re.fullmatch(r"config_generation\.[\w.-]+\.yaml", name):
        raise ConfigError("Unknown backup file")
    if not path.exists():
        raise ConfigError("Backup not found")
    return path


def save_text(text: str, base_sha: str | None) -> dict[str, Any]:
    """Validate, back up the current file and write ``text``.

    Raises:
        ConfigError: If the text is invalid or the file changed since ``base_sha`` was read.
    """
    current = read_config()
    if base_sha and base_sha != current.sha:
        raise ConfigError("config_generation.yaml was changed on disk since you opened it. Reload and re-apply your edits.",
                          [{"level": "error", "message": "The file changed on disk (conflict).", "conflict": True}])
    if text == current.text:
        return {"saved": False, "backup": None, "sha": current.sha, "issues": []}
    _, issues = validate_text(text)
    errors = [i for i in issues if i["level"] == "error"]
    if errors:
        raise ConfigError(errors[0]["message"], issues)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = paths().backups_dir / f"config_generation.{stamp}.yaml"
    counter = 1
    while backup.exists():
        backup = paths().backups_dir / f"config_generation.{stamp}-{counter}.yaml"
        counter += 1
    shutil.copy2(paths().config_file, backup)
    tmp = paths().config_file.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(paths().config_file)
    for old in list_backups()[MAX_BACKUPS:]:
        (paths().backups_dir / old["name"]).unlink(missing_ok=True)
    return {"saved": True, "backup": backup.name, "sha": sha(text), "issues": issues}
