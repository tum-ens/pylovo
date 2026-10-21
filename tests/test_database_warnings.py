"""Importing the database package must not hide the UserWarnings of pylovo or of the host process."""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

CODE = '''
import json
import warnings

import pandas as pd

import pylovo.database.database_client      # registers the warning filters of the database package

with warnings.catch_warnings(record=True) as caught:      # import-time filters stay in place (pytest would undo them)
    warnings.warn("K-means clustering issue: 3 buildings not assigned to clusters", UserWarning)
    warnings.warn("transformers table is overwritten!", UserWarning)
    try:
        pd.read_sql_query("SELECT 1", object())             # pandas warns about a connection it does not know
    except Exception:
        pass
print(json.dumps([str(w.message) for w in caught]))
'''


def test_only_the_pandas_connection_warning_is_hidden():
    proc = subprocess.run([sys.executable, "-c", CODE], cwd=REPO, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    messages = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "K-means clustering issue: 3 buildings not assigned to clusters" in messages
    assert "transformers table is overwritten!" in messages
    assert not [m for m in messages if m.startswith("pandas only supports")]
