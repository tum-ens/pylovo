"""Run the tutorial notebooks and store their outputs, small and free of local details; for maintainers.

From the repository root::

    uv run --extra notebooks python notebook_tutorials/refresh_outputs.py                     # the learning path
    uv run --extra notebooks python notebook_tutorials/refresh_outputs.py 03_one_grid.ipynb   # one notebook

Every notebook runs with nbclient in its folder and without a database (``PYLOVO_ENV_FILE`` empty), so its outputs
come from the example data in ``data/examples`` and are the same for everyone. Afterwards the script

* stores every PNG as an 8-bit palette image when that is smaller: the figures use few colours, so they shrink to
  about a third without a visible change,
* removes the execution timestamps and keeps the notebook metadata minimal,
* refuses outputs with an error, a home path, an IP address or the words password or secret,
* and refuses a notebook above 1000 kB, the limit of the pre-commit hook.
"""
from __future__ import annotations

import argparse
import base64
import io
import os
import re
import sys
from pathlib import Path

import nbformat
from nbclient import NotebookClient
from PIL import Image

FOLDER = Path(__file__).resolve().parent
LEARNING_PATH = ["01_tour.ipynb", "02_building_grids.ipynb", "03_one_grid.ipynb", "04_comparing_areas.ipynb",
                 "05_versions.ipynb", "06_voltage_levels.ipynb"]
MAX_KB = 1000
FORBIDDEN = [re.compile(p) for p in (r"/home/[\w.-]+", r"/Users/[\w.-]+", r"[A-Za-z]:\\Users\\",
                                     r"\b(?:\d{1,3}\.){3}\d{1,3}\b", r"(?i)password", r"(?i)secret")]
KERNELSPEC = {"display_name": "Python 3 (ipykernel)", "language": "python", "name": "python3"}


def _palette_png(data: str) -> str:
    """The PNG (base64) as an 8-bit palette image if that is smaller, else unchanged."""
    raw = base64.b64decode(data)
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    quantized = image.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    buffer = io.BytesIO()
    quantized.save(buffer, format="PNG", optimize=True)
    return base64.b64encode(buffer.getvalue()).decode() if buffer.tell() < len(raw) else data


def _texts(output) -> list[str]:
    texts = [output.get("text", "")]
    for kind, value in output.get("data", {}).items():
        if kind.startswith("text/"):
            texts.append(value if isinstance(value, str) else "".join(value))
    return texts


def refresh(path: Path) -> int:
    """Run one notebook, clean its outputs and write it back; returns its size in kB."""
    notebook = nbformat.read(path, as_version=4)
    NotebookClient(notebook, timeout=900, kernel_name="python3",
                   resources={"metadata": {"path": str(path.parent)}}).execute()
    problems = []
    for index, cell in enumerate(notebook.cells):
        cell.metadata.pop("execution", None)
        for output in cell.get("outputs", []):
            if output.output_type == "error":
                problems.append(f"cell {index}: {output.ename}: {output.evalue}")
            if output.output_type == "stream" and output.get("name") == "stderr":
                problems.append(f"cell {index}: writes to stderr: {output.text[:200]!r}")
            for text in _texts(output):
                problems += [f"cell {index}: {pattern.pattern} in the output"
                             for pattern in FORBIDDEN if pattern.search(text)]
            if "image/png" in output.get("data", {}):
                output.data["image/png"] = _palette_png(output.data["image/png"])
    notebook.metadata = {"kernelspec": KERNELSPEC, "language_info": {"name": "python"}}
    if problems:
        raise SystemExit(f"{path.name}: not stored\n  " + "\n  ".join(problems))
    nbformat.write(notebook, path)
    size = path.stat().st_size // 1024
    if size > MAX_KB:
        raise SystemExit(f"{path.name}: {size} kB, more than {MAX_KB} kB")
    return size


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("notebooks", nargs="*", default=LEARNING_PATH, help="notebooks of this folder")
    args = parser.parse_args()
    os.environ["PYLOVO_ENV_FILE"] = ""       # the kernels inherit it: the example data, never a database
    for name in args.notebooks:
        size = refresh(FOLDER / name)
        print(f"{name}: {size} kB")
    sys.exit(0)


if __name__ == "__main__":
    main()
