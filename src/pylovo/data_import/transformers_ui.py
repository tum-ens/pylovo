"""Browser UI for editing transformer positions (``pylovo-import transformers-ui``).

**Deprecated**: the GridPlanner UI (backed by ``pylovo-api``, ``uv sync --extra api``) covers all of
its features in its transformer editor. This Flask map is kept for now; it needs the optional
extra ``legacy-ui`` (``uv sync --extra legacy-ui``) and will be removed in a later release.

A small Flask app with a Leaflet map: select a PLZ, add or delete transformer positions and set
their rated power one by one or in bulk. All database access goes through the ``*_trafo_ui``
methods of :class:`~pylovo.database.database_client.DatabaseClient`; every request opens and
closes its own connection.

The HTML/JavaScript page is kept inline in :meth:`TransformerMapUI._get_html_template` because
the wheel only packages ``*.py`` files.

Usage::

    pylovo-import transformers-ui [--host HOST] [--port PORT] [--debug] [--auto-cleanup]
    python -m pylovo.data_import.transformers_ui [--host HOST] [--port PORT]

The server binds to ``127.0.0.1`` by default. Binding to ``0.0.0.0`` exposes write access to the
``transformers`` table to everyone who can reach the port.
"""

import argparse
import logging
import socket
import subprocess
import sys
import time
import warnings
from contextlib import contextmanager

try:  # optional since the deprecation: pylovo-import works without Flask
    from flask import Flask, Response, jsonify, request
except ImportError:  # pragma: no cover - depends on the installed extras
    Flask = Response = jsonify = request = None

DEPRECATION_MESSAGE = (
    "pylovo-import transformers-ui is deprecated and will be removed in a later release: "
    "use the transformer editor of the GridPlanner UI (backed by pylovo-api: uv sync --extra api)."
)

from pylovo.config_loader import DBNAME, DBUSER, HOST, PASSWORD, PORT
from pylovo.data_import.transformer_capacity_utils import get_transformer_capacity_options
from pylovo.database.database_client import DatabaseClient

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080


def _ok(**payload) -> Response:
    """Return a successful JSON API response."""
    return jsonify({"success": True, **payload})


def _fail(error) -> Response:
    """Return a failed JSON API response with an error message."""
    return jsonify({"success": False, "error": str(error)})


class TransformerMapUI:
    """Flask app that serves the transformer map page and its JSON API.

    Args:
        host: Interface the server binds to. ``127.0.0.1`` keeps the UI local.
        port: TCP port of the server.

    Raises:
        ConnectionError: If the database configured in ``.env`` cannot be reached at start-up.
    """

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host = host
        self.port = port
        self.app = Flask(__name__)
        # Connection parameters; a new DatabaseClient is created per request (thread safety).
        self.db_params = {
            'dbname': DBNAME,
            'user': DBUSER,
            'pw': PASSWORD,
            'host': HOST,
            'port': PORT
        }

        if not self._test_database_connection():
            raise ConnectionError(f"Database connection test failed for {DBNAME} on {HOST}:{PORT}")
        print("✓ Database connection test successful")

        self._setup_routes()

    def _test_database_connection(self) -> bool:
        """Return whether a database connection can be opened and answers ``SELECT 1``."""
        try:
            with self._get_db_client():
                return True
        except Exception as e:
            logger.error("Database connection test failed: %s", e)
            return False

    @contextmanager
    def _get_db_client(self, max_retries: int = 3):
        """Yield a healthy :class:`DatabaseClient` and close it afterwards.

        Args:
            max_retries: Number of connection attempts (one second apart) before giving up.

        Raises:
            Exception: The last connection error if no healthy connection could be opened.
        """
        dbc = None
        last_exception = None

        for attempt in range(max_retries):
            try:
                dbc = DatabaseClient(**self.db_params)
                if self._is_connection_healthy(dbc):
                    break
                logger.warning("Connection health check failed (attempt %d/%d)", attempt + 1, max_retries)
            except Exception as e:
                last_exception = e
                logger.warning("Database connection attempt %d/%d failed: %s", attempt + 1, max_retries, e)
            if dbc is not None:
                self._close_quietly(dbc)
                dbc = None
            if attempt < max_retries - 1:
                time.sleep(1)

        if dbc is None:
            if last_exception:
                raise last_exception
            raise ConnectionError("Failed to establish a healthy database connection after all retries")

        try:
            yield dbc
        finally:
            self._close_quietly(dbc)

    @staticmethod
    def _close_quietly(dbc) -> None:
        """Close a database client and ignore errors while doing so."""
        try:
            dbc.close()
        except Exception:
            pass

    @staticmethod
    def _is_connection_healthy(dbc) -> bool:
        """Return whether ``dbc`` has an open cursor that answers ``SELECT 1``."""
        try:
            if not dbc or not getattr(dbc, 'cur', None):
                return False
            dbc.cur.execute("SELECT 1")
            result = dbc.cur.fetchone()
            return result is not None and result[0] == 1
        except Exception as e:
            logger.warning("Connection health check failed: %s", e)
            return False

    def _setup_routes(self):
        """Register the page route and the JSON API routes."""

        @self.app.route('/')
        def index():
            """Main page with the map interface."""
            return self._get_html_template()

        @self.app.route('/api/plz-list')
        def get_plz_list():
            """List the PLZ codes available in the database."""
            try:
                with self._get_db_client() as dbc:
                    return _ok(plz_list=dbc.get_available_plz_list_trafo_ui())
            except Exception as e:
                return _fail(e)

        @self.app.route('/api/transformer-capacities')
        def get_transformer_capacities():
            """List the transformer ratings defined in config_generation.yaml."""
            try:
                return _ok(capacities=get_transformer_capacity_options())
            except Exception as e:
                return _fail(e)

        @self.app.route('/api/transformer-positions/<int:plz>')
        def get_transformer_positions(plz):
            """List the transformers that lie inside the PLZ area."""
            try:
                with self._get_db_client() as dbc:
                    return _ok(positions=dbc.get_transformer_positions_for_plz_trafo_ui(plz))
            except Exception as e:
                return _fail(e)

        @self.app.route('/api/plz-bounds/<int:plz>')
        def get_plz_bounds(plz):
            """Return the bounding box of the PLZ area."""
            try:
                with self._get_db_client() as dbc:
                    bounds = dbc.get_plz_bounds_trafo_ui(plz)
                if bounds:
                    return _ok(bounds=bounds)
                return _fail("PLZ not found")
            except Exception as e:
                return _fail(e)

        @self.app.route('/api/add-transformer', methods=['POST'])
        def add_transformer():
            """Add a transformer; without ``osm_id`` a ``manual/<unix time in ms>`` id is generated."""
            try:
                data = request.get_json()
                plz = int(data['plz'])
                geom_wkt = data['geom_wkt']
                osm_id = data.get('osm_id') or f"manual/{time.time_ns() // 1_000_000}"
                transformer_rated_power = data.get('transformer_rated_power')

                with self._get_db_client() as dbc:
                    result_osm_id = dbc.add_transformer_position_trafo_ui(
                        plz=plz,
                        geom_wkt=geom_wkt,
                        osm_id=osm_id,
                        transformer_rated_power=transformer_rated_power
                    )
                return _ok(osm_id=result_osm_id)
            except Exception as e:
                return _fail(e)

        @self.app.route('/api/delete-transformer', methods=['POST'])
        def delete_transformer():
            """Delete a transformer by its ``osm_id``."""
            try:
                osm_id = request.get_json()['osm_id']
                logger.debug("Deletion request for OSM ID %s", osm_id)
                with self._get_db_client() as dbc:
                    success = dbc.delete_transformer_by_osm_id_trafo_ui(osm_id)
                logger.debug("Deletion result for %s: %s", osm_id, success)
                return _ok() if success else _fail("Transformer not found")
            except Exception as e:
                logger.warning("Deleting a transformer failed: %s", e)
                return _fail(e)

        @self.app.route('/api/update-transformer-capacity', methods=['POST'])
        def update_transformer_capacity():
            """Set the rated power (kVA) of one transformer."""
            try:
                data = request.get_json()
                osm_id = data['osm_id']
                transformer_rated_power = data['transformer_rated_power']
                with self._get_db_client() as dbc:
                    success = dbc.update_transformer_capacity_trafo_ui(osm_id, transformer_rated_power)
                return _ok() if success else _fail("Transformer not found")
            except Exception as e:
                return _fail(e)

        @self.app.route('/api/bulk-update-capacities', methods=['POST'])
        def bulk_update_capacities():
            """Set the rated power of all transformers in a PLZ (``uniform`` or ``percentage``)."""
            try:
                data = request.get_json()
                logger.debug("Bulk update request: %s", data)
                plz = int(data['plz'])
                distribution_method = data['distribution_method']

                with self._get_db_client() as dbc:
                    if distribution_method == 'uniform':
                        # Same rating for every transformer in the PLZ.
                        transformer_rated_power = int(data['transformer_rated_power'])
                        success = dbc.bulk_update_capacities_uniform_trafo_ui(plz, transformer_rated_power)
                    elif distribution_method == 'percentage':
                        # Ratings drawn according to the given percentage shares.
                        capacity_distribution = data['capacity_distribution']
                        success = dbc.bulk_update_capacities_percentage_trafo_ui(plz, capacity_distribution)
                    else:
                        return _fail("Invalid distribution method")

                logger.debug("Bulk update result for PLZ %s: %s", plz, success)
                return _ok() if success else _fail("Failed to update capacities")
            except Exception as e:
                logger.warning("Bulk capacity update failed: %s", e)
                return _fail(e)

        @self.app.route('/api/clear-capacities', methods=['POST'])
        def clear_capacities():
            """Remove the rated power of all transformers in a PLZ."""
            try:
                plz = int(request.get_json()['plz'])
                logger.debug("Clear capacities request for PLZ %s", plz)
                with self._get_db_client() as dbc:
                    success = dbc.clear_capacities_trafo_ui(plz)
                logger.debug("Clear capacities result for PLZ %s: %s", plz, success)
                return _ok() if success else _fail("Failed to clear capacities")
            except Exception as e:
                logger.warning("Clearing capacities failed: %s", e)
                return _fail(e)

    def _get_html_template(self) -> str:
        """Return the single-page HTML/JavaScript UI (Leaflet map, PLZ search, bulk tools)."""
        return r'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Transformer Map UI</title>
        <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
        <meta http-equiv="Pragma" content="no-cache">
        <meta http-equiv="Expires" content="0">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <style>
        body {
            margin: 0;
            padding: 0;
            font-family: Arial, sans-serif;
        }
        #map {
            height: 100vh;
            width: 100%;
        }
        .control-panel {
            position: absolute;
            top: 10px;
            right: 10px;
            background: white;
            padding: 15px;
            border-radius: 5px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            z-index: 1000;
            min-width: 250px;
        }
        .control-panel h3 {
            margin-top: 0;
            color: #333;
        }
        .form-group {
            margin-bottom: 10px;
        }
        .form-group label {
            display: block;
            margin-bottom: 5px;
            font-weight: bold;
        }
        .form-group select, .form-group input {
            width: 100%;
            padding: 5px;
            border: 1px solid #ddd;
            border-radius: 3px;
        }
        .btn {
            background: #007cba;
            color: white;
            border: none;
            padding: 8px 15px;
            border-radius: 3px;
            cursor: pointer;
            margin-right: 5px;
        }
        .btn:hover {
            background: #005a87;
        }
        .btn-danger {
            background: #dc3545;
        }
        .btn-danger:hover {
            background: #c82333;
        }
        .status {
            margin-top: 10px;
            padding: 5px;
            border-radius: 3px;
            font-size: 12px;
        }
        .status.success {
            background: #d4edda;
            color: #155724;
            border: 1px solid #c3e6cb;
        }
        .status.error {
            background: #f8d7da;
            color: #721c24;
            border: 1px solid #f5c6cb;
        }
        .status.info {
            background: #d1ecf1;
            color: #0c5460;
            border: 1px solid #bee5eb;
        }
        .transformer-popup {
            font-size: 12px;
        }
        .transformer-popup h4 {
            margin: 0 0 5px 0;
            color: #333;
        }
        .transformer-popup p {
            margin: 2px 0;
            color: #666;
        }
        .panel-header {
            border-bottom: 2px solid #e9ecef;
            padding-bottom: 10px;
            margin-bottom: 15px;
        }
        .current-plz {
            background: #e3f2fd;
            color: #1976d2;
            padding: 5px 10px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: bold;
            margin-top: 5px;
        }
        .form-section {
            margin-bottom: 20px;
            padding: 15px;
            background: #f8f9fa;
            border-radius: 6px;
            border-left: 4px solid #007bff;
        }
        .form-section h4 {
            margin: 0 0 10px 0;
            color: #495057;
            font-size: 14px;
            font-weight: 600;
        }
        .button-group {
            display: flex;
            gap: 8px;
            margin: 10px 0;
        }
        .button-group .btn {
            flex: 1;
        }
        .help-text {
            display: block;
            font-size: 11px;
            color: #6c757d;
            margin-top: 3px;
        }
        .status-indicator {
            font-size: 12px;
            color: #666;
            margin-top: 8px;
            padding: 4px 8px;
            background: #f8f9fa;
            border-radius: 3px;
            border: 1px solid #dee2e6;
        }
        .instructions {
            background: #fff3cd;
            border: 1px solid #ffeaa7;
            border-radius: 6px;
            padding: 12px;
            margin-top: 15px;
        }
        .instructions h4 {
            margin: 0 0 8px 0;
            color: #856404;
            font-size: 13px;
        }
        .instructions ul {
            margin: 0;
            padding-left: 15px;
        }
        .instructions li {
            font-size: 11px;
            color: #856404;
            margin: 3px 0;
        }
        .legend {
            position: absolute;
            bottom: 20px;
            right: 20px;
            background: white;
            padding: 10px;
            border-radius: 6px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.2);
            font-size: 12px;
            z-index: 1000;
        }
        .legend h4 {
            margin: 0 0 8px 0;
            color: #333;
            font-size: 13px;
        }
        .legend-item {
            display: flex;
            align-items: center;
            margin: 4px 0;
        }
        .legend-marker {
            width: 16px;
            height: 16px;
            margin-right: 8px;
            border-radius: 50%;
            display: inline-block;
        }
        .legend-marker.blue {
            background: #007bff;
        }
        .legend-marker.red {
            background: #dc3545;
        }
        
        /* Bulk Management Styles */
        .radio-group {
            display: flex;
            flex-direction: row;
            gap: 20px;
            margin: 8px 0;
            padding: 4px 8px;
            background-color: #f8f9fa;
            border-radius: 6px;
            border: 1px solid #dee2e6;
        }
        
        .radio-label {
            display: flex;
            align-items: center;
            cursor: pointer;
            padding: 2px 4px;
            border-radius: 4px;
            transition: all 0.2s;
            font-size: 12px;
            font-weight: 500;
            white-space: nowrap;
        }
        
        .radio-label:hover {
            background-color: #e9ecef;
        }
        
        .radio-label input[type="radio"] {
            margin-right: 4px;
            margin-left: 0;
        }
        
        .radio-label input[type="radio"]:checked + span {
            color: #007bff;
            font-weight: 600;
        }
        
        .distribution-panel {
            margin: 15px 0;
            padding: 15px;
            border: 1px solid #dee2e6;
            border-radius: 6px;
            background-color: #f8f9fa;
        }
        
        .capacity-percentage-item {
            display: flex;
            align-items: center;
            margin: 8px 0;
            gap: 10px;
        }
        
        .capacity-percentage-item label {
            min-width: 120px;
            font-size: 14px;
        }
        
        .percentage-slider {
            flex: 1;
            margin: 0 10px;
            -webkit-appearance: none;
            appearance: none;
            height: 8px;
            border-radius: 4px;
            background: #e9ecef;
            outline: none;
            position: relative;
        }
        
        .percentage-slider::-webkit-slider-track {
            width: 100%;
            height: 8px;
            cursor: pointer;
            background: #e9ecef;
            border-radius: 4px;
        }
        
        .percentage-slider::-webkit-slider-thumb {
            -webkit-appearance: none;
            appearance: none;
            width: 0px;
            height: 0px;
            border-radius: 50%;
            background: #007bff;
            cursor: pointer;
            border: 3px solid #fff;
            box-shadow: 0 2px 6px rgba(0,0,0,0.3);
            position: relative;
            z-index: 2;
        }
        
        .percentage-slider::-moz-range-track {
            width: 100%;
            height: 8px;
            cursor: pointer;
            background: #e9ecef;
            border-radius: 4px;
            border: none;
        }
        
        .percentage-slider::-moz-range-thumb {
            width: 0px;
            height: 0px;
            border-radius: 50%;
            background: #007bff;
            cursor: pointer;
            border: 3px solid #fff;
            box-shadow: 0 2px 6px rgba(0,0,0,0.3);
        }
        
        .percentage-slider:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        
        /* Enhanced slider with visual fill */
        .slider-container {
            position: relative;
            flex: 1;
            margin: 0 10px;
            height: 20px;
            background: #e9ecef;
            border-radius: 4px;
            overflow: visible;
            padding: 0 8px;
        }
        
        .slider-fill {
            position: absolute;
            top: 6px;
            left: 8px;
            height: 8px;
            background: #007bff;
            border-radius: 4px;
            transition: width 0.1s ease;
            width: 0%;
        }
        
        .slider-thumb {
            position: absolute;
            top: 50%;
            left: 8px;
            width: 16px;
            height: 16px;
            background: #007bff;
            border: 2px solid #fff;
            border-radius: 50%;
            transform: translate(0%, -50%);
            cursor: pointer;
            z-index: 3;
            transition: left 0.1s ease;
        }
        
        .percentage-slider {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            -webkit-appearance: none;
            appearance: none;
            background: transparent;
            cursor: pointer;
            z-index: 1;
            pointer-events: none;
        }
        
        .percentage-slider::-webkit-slider-track {
            width: 100%;
            height: 8px;
            cursor: pointer;
            background: transparent;
            border-radius: 4px;
        }
        
        .percentage-slider::-webkit-slider-thumb {
            -webkit-appearance: none;
            appearance: none;
            width: 0px;
            height: 0px;
            border-radius: 50%;
            background: transparent;
            cursor: pointer;
            border: none;
        }
        
        .percentage-slider::-moz-range-track {
            width: 100%;
            height: 8px;
            cursor: pointer;
            background: transparent;
            border-radius: 4px;
            border: none;
        }
        
        .percentage-slider::-moz-range-thumb {
            width: 0px;
            height: 0px;
            border-radius: 50%;
            background: transparent;
            cursor: pointer;
            border: none;
        }
        
        .percentage-slider:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        
        .percentage-value {
            min-width: 40px;
            text-align: right;
            font-weight: bold;
            color: #495057;
        }
        
        .percentage-summary {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-top: 15px;
            padding: 10px;
            background-color: #e9ecef;
            border-radius: 4px;
        }
        
        .btn-small {
            padding: 4px 8px;
            font-size: 12px;
            background: #6c757d;
            color: white;
            border: none;
            border-radius: 3px;
            cursor: pointer;
        }
        
        .btn-small:hover {
            background: #5a6268;
        }
        
        .bulk-status {
            margin-top: 15px;
            padding: 10px;
            background-color: #e9ecef;
            border-radius: 4px;
            font-size: 14px;
        }
        
        .bulk-status div {
            margin: 2px 0;
        }
        
        #transformer-count {
            font-weight: bold;
            color: #495057;
        }
    </style>
</head>
<body>
    <div id="map">
        <div class="legend">
            <h4>Transformer Types</h4>
            <div class="legend-item">
                <div class="legend-marker blue"></div>
                <span>OSM imported Transformers</span>
            </div>
            <div class="legend-item">
                <div class="legend-marker red"></div>
                <span>Manually added Transformers</span>
            </div>
        </div>
    </div>
    <div class="control-panel">
        <div class="panel-header">
            <h3>Transformer Management</h3>
            <div id="current-plz-display" class="current-plz">No PLZ loaded</div>
        </div>
        
        <div class="form-section">
            <h4>Load Area</h4>
            <div class="form-group">
                <label for="plz-input">PLZ Code:</label>
                <input type="number" id="plz-input" placeholder="Enter German PLZ (e.g., 10115)" min="10000" max="99999">
            </div>
            <div class="button-group">
                <button class="btn" onclick="loadPLZData()">Load Data</button>
                <button class="btn" onclick="clearMap()">Clear Map</button>
            </div>
        </div>
        
        <div class="form-section">
            <h4>Add New Transformer</h4>
            <div class="button-group">
                <button id="toggle-add-mode" class="btn" onclick="toggleAddingMode()" style="background: #28a745;">Start Adding Transformers</button>
            </div>
            <div id="add-mode-status" class="status-indicator">Adding mode: OFF</div>
            <div class="form-group">
                <label for="capacity-select">Transformer Capacity:</label>
                <select id="capacity-select">
                    <option value="">Select capacity...</option>
                </select>
            </div>
        </div>
        
        <div class="form-section">
            <h4>Bulk Capacity Management</h4>
            <div class="form-group">
                <label>Distribution Method:</label>
                <div class="radio-group">
                    <label class="radio-label">
                        <input type="radio" name="distribution-method" value="uniform" checked>
                        <span>Uniform</span>
                    </label>
                    <label class="radio-label">
                        <input type="radio" name="distribution-method" value="percentage">
                        <span>Percentage Mix</span>
                    </label>
                </div>
            </div>
            
            <div id="uniform-distribution" class="distribution-panel">
                <div class="form-group">
                    <label for="bulk-capacity-select">Set All Transformers to:</label>
                    <select id="bulk-capacity-select">
                        <option value="">Select capacity...</option>
                    </select>
                </div>
                <button id="apply-uniform" class="btn" onclick="applyUniformCapacity()" style="background: #007bff;">Apply to All Transformers</button>
            </div>
            
            <div id="percentage-distribution" class="distribution-panel" style="display: none;">
                <div class="form-group">
                    <label>Capacity Distribution (%):</label>
                    <div id="capacity-percentages">
                        <!-- Will be populated dynamically -->
                    </div>
                    <div class="percentage-summary">
                        <span id="total-percentage">Total: 0%</span>
                        <button id="reset-percentages" class="btn-small" onclick="resetPercentages()">Reset</button>
                    </div>
                </div>
                <button id="apply-percentage" class="btn" onclick="applyPercentageDistribution()" style="background: #6f42c1;">Apply Distribution Randomly</button>
            </div>
            
            <div class="bulk-status">
                <div id="bulk-status-text"></div>
                <div id="transformer-count">Transformers in area: 0</div>
            </div>
            
            <div class="form-group" style="margin-top: 15px;">
                <button id="clear-capacities" class="btn" onclick="clearAllCapacities()" style="background: #dc3545; color: white;">
                    Clear All Capacities
                </button>
            </div>
        </div>
        
        <div class="instructions">
            <h4>Instructions:</h4>
            <ul>
                <li>Enter any German PLZ (10000-99999)</li>
                <li>Click "Load Data" to view transformers in that area</li>
                <li>Click "Start Adding Transformers" to enable adding mode</li>
                <li>Click on map to add new transformer</li>
                <li>Click on existing transformer to edit/delete</li>
                <li><strong>Bulk Management:</strong> Set all transformers to same capacity or use percentage distribution</li>
                <li>Use percentage sliders to define capacity mix (must total 100%)</li>
            </ul>
        </div>
        <div id="status"></div>
    </div>

    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script>
        let map;
        let transformerLayer;
        let currentPLZ = null;
        let isAddingMode = false;
        let currentPLZDisplay = null;

        // Initialize map
        function initMap() {
            map = L.map('map', {
                center: [51.1657, 10.4515],
                zoom: 6,
                zoomControl: true,
                scrollWheelZoom: true,
                doubleClickZoom: true,
                boxZoom: true,
                keyboard: true,
                dragging: true,
                zoomSnap: 0.25,
                zoomDelta: 0.5
            });
            
            L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
                attribution: '© OpenStreetMap contributors',
                maxZoom: 18,
                minZoom: 1,
                tileSize: 256,
                zoomOffset: 0,
                updateWhenIdle: true,
                keepBuffer: 2
            }).addTo(map);
            
            transformerLayer = L.layerGroup().addTo(map);
            
            // Add click handler for map
            map.on('click', async function(e) {
                console.log('Map clicked!', { isAddingMode, currentPLZ, lat: e.latlng.lat, lng: e.latlng.lng });
                if (!isAddingMode || !currentPLZ) {
                    console.log('Click ignored - not in adding mode or no PLZ selected');
                    return;
                }
                
                const lat = e.latlng.lat;
                const lng = e.latlng.lng;
                const geomWkt = `POINT(${lng} ${lat})`;
                
                const capacity = document.getElementById('capacity-select').value;
                console.log('Adding transformer with:', { capacity, geomWkt });
                
                try {
                    console.log('Sending request with:', { plz: currentPLZ, geom_wkt: geomWkt, transformer_rated_power: capacity });
                    const response = await fetch('/api/add-transformer', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                        },
                        body: JSON.stringify({
                            plz: currentPLZ,
                            geom_wkt: geomWkt,
                            transformer_rated_power: capacity ? parseInt(capacity) : null
                        })
                    });
                    
                    const data = await response.json();
                    console.log('Response from server:', data);
                    
                    if (data.success) {
                        showStatus('Transformer added successfully', 'success');
                        // Add the new transformer to the map without reloading
                        console.log('Adding transformer marker immediately:', {
                            osm_id: data.osm_id,
                            capacity: capacity,
                            geomWkt: geomWkt
                        });
                        addTransformerMarker({
                            osm_id: data.osm_id,
                            area: null,
                            transformer_rated_power: capacity ? parseInt(capacity) : null,
                            type: 'Manual',
                            geom_type: 'POINT',
                            within_shopping: false,
                            geom_wkt: geomWkt
                        });
                    } else {
                        console.error('Error adding transformer:', data.error);
                        showStatus('Error adding transformer: ' + data.error, 'error');
                    }
                } catch (error) {
                    showStatus('Error adding transformer: ' + error.message, 'error');
                }
            });
        }

        // Load available PLZ codes
        async function loadPLZList() {
            try {
                console.log('Loading PLZ list...');
                const response = await fetch('/api/plz-list');
                console.log('Response status:', response.status);
                const data = await response.json();
                console.log('PLZ data:', data);

                if (data.success) {
                    // PLZ list is loaded but not displayed in UI anymore
                    console.log('PLZ list loaded:', data.plz_list.length, 'PLZs available');
                } else {
                    showStatus('Error: ' + data.error, 'error');
                    console.error('API error:', data.error);
                }
            } catch (error) {
                showStatus('Error: ' + error.message, 'error');
                console.error('Fetch error:', error);
            }
        }

        async function loadTransformerCapacities() {
            try {
                console.log('Loading transformer capacities...');
                const response = await fetch('/api/transformer-capacities');
                const data = await response.json();

                if (data.success) {
                    const select = document.getElementById('capacity-select');
                    select.innerHTML = '<option value="">Select capacity...</option>';
                    data.capacities.forEach(capacity => {
                        const option = document.createElement('option');
                        option.value = capacity.value;
                        option.textContent = capacity.label;
                        select.appendChild(option);
                    });
                    console.log(`Loaded ${data.capacities.length} transformer capacities`);
                } else {
                    console.error('Error loading transformer capacities:', data.error);
                }
            } catch (error) {
                console.error('Error loading transformer capacities:', error);
            }
        }

        // Load data for selected PLZ
        async function loadPLZData() {
            const plzInput = document.getElementById('plz-input');
            let plz = plzInput.value;
            
            // If input is empty but we have a current PLZ, use that instead
            if (!plz && currentPLZ) {
                plz = currentPLZ.toString();
            }
            
            if (!plz) {
                showStatus('Please enter a PLZ code', 'error');
                return;
            }
            
            // Validate PLZ (German PLZ range: 10000-99999)
            const plzNum = parseInt(plz);
            if (plzNum < 10000 || plzNum > 99999) {
                showStatus('Please enter a valid German PLZ (10000-99999)', 'error');
                return;
            }
            
            // Don't automatically enable adding mode - let user toggle it
            
            try {
                // Clear map first (but preserve PLZ)
                transformerLayer.clearLayers();
                isAddingMode = false;
                document.getElementById('plz-input').value = '';
                document.getElementById('capacity-select').value = '';
                
                // Reset adding mode button
                const button = document.getElementById('toggle-add-mode');
                const status = document.getElementById('add-mode-status');
                button.style.background = '#28a745';
                button.textContent = 'Start Adding Transformers';
                status.textContent = 'Adding mode: OFF';
                status.style.color = '#666';
                
                // Set PLZ after clearing
                currentPLZ = plzNum;
                currentPLZDisplay = plzNum;
                
                // Load PLZ bounds (if available in database)
                const boundsResponse = await fetch(`/api/plz-bounds/${plz}`);
                const boundsData = await boundsResponse.json();
                
                if (boundsData.success) {
                    const bounds = boundsData.bounds;
                    console.log('PLZ bounds:', bounds);
                    console.log('Fitting bounds to:', [[bounds.miny, bounds.minx], [bounds.maxy, bounds.maxx]]);
                    map.fitBounds([[bounds.miny, bounds.minx], [bounds.maxy, bounds.maxx]]);
                } else {
                    // If no bounds in database, center on a rough estimate for German PLZ
                    // This is a very rough approximation - in practice you'd want a proper geocoding service
                    const lat = 50.0 + (plzNum % 1000) / 1000.0; // Very rough latitude
                    const lng = 10.0 + (plzNum / 1000) / 100.0; // Very rough longitude
                    map.setView([lat, lng], 12);
                }
                
                // Load transformer positions
                const response = await fetch(`/api/transformer-positions/${plz}`);
                const data = await response.json();
                
                if (data.success) {
                    // Add transformers
                    data.positions.forEach(pos => {
                        addTransformerMarker(pos);
                    });
                    
                    // Update PLZ display after successful load
                    document.getElementById('current-plz-display').textContent = `Current PLZ: ${plzNum}`;
                    document.getElementById('current-plz-display').style.background = '#d4edda';
                    document.getElementById('current-plz-display').style.color = '#155724';
                    
                    // Update transformer count for bulk management
                    updateTransformerCount(data.positions.length);
                    
                    showStatus(`Loaded ${data.positions.length} transformer positions for PLZ ${plz}`, 'success');
                } else {
                    showStatus('Error loading transformer positions: ' + data.error, 'error');
                }
            } catch (error) {
                showStatus('Error loading data: ' + error.message, 'error');
            }
        }


        // Add transformer marker to map
        function addTransformerMarker(position) {
            console.log('addTransformerMarker called with:', position);
            console.log('WKT string:', position.geom_wkt);
            console.log('WKT string length:', position.geom_wkt.length);
            console.log('WKT string type:', typeof position.geom_wkt);
            
            // Parse WKT geometry to get coordinates (handle both POINT and MULTIPOINT)
            // Try single parentheses first (for manually added transformers)
            let wktMatch = position.geom_wkt.match(/POINT\(([^)]+)\)/);
            console.log('POINT (single) match:', wktMatch);
            
            if (!wktMatch) {
                // Try double parentheses (for existing transformers)
                wktMatch = position.geom_wkt.match(/POINT\(\(([^)]+)\)\)/);
                console.log('POINT (double) match:', wktMatch);
            }
            
            if (!wktMatch) {
                // Try MULTIPOINT with single parentheses (no space)
                wktMatch = position.geom_wkt.match(/MULTIPOINT\(([^)]+)\)/);
                console.log('MULTIPOINT (single, no space) match:', wktMatch);
            }
            
            if (!wktMatch) {
                // Try MULTIPOINT with double parentheses (no space)
                wktMatch = position.geom_wkt.match(/MULTIPOINT\(\(([^)]+)\)\)/);
                console.log('MULTIPOINT (double, no space) match:', wktMatch);
            }
            
            if (!wktMatch) {
                // Try MULTIPOINT with single parentheses (with space)
                wktMatch = position.geom_wkt.match(/MULTIPOINT\s+\(([^)]+)\)/);
                console.log('MULTIPOINT (single, with space) match:', wktMatch);
            }
            
            if (!wktMatch) {
                // Try MULTIPOINT with double parentheses (with space)
                wktMatch = position.geom_wkt.match(/MULTIPOINT\s+\(\(([^)]+)\)\)/);
                console.log('MULTIPOINT (double, with space) match:', wktMatch);
            }
            
            if (!wktMatch) {
                console.error('Could not parse WKT:', position.geom_wkt);
                return;
            }
            
            console.log('Matched group:', wktMatch[1]);
            
            // Clean the matched group by removing any leading/trailing parentheses and whitespace
            const cleanCoords = wktMatch[1].replace(/^[\(\s]+|[\)\s]+$/g, '');
            console.log('Cleaned coordinates:', cleanCoords);
            
            // Split coordinates and parse
            const coords = cleanCoords.split(/\s+/).map(coord => parseFloat(coord.trim()));
            console.log('Parsed coordinates:', coords);
            
            if (coords.length !== 2 || isNaN(coords[0]) || isNaN(coords[1])) {
                console.error('Invalid coordinates:', coords);
                return;
            }
            
            const [lng, lat] = coords;
            // WKT format is (lng lat), but Leaflet expects [lat, lng]
            console.log('Creating marker at:', [lat, lng]);
            
            // Determine if this is a manually added transformer
            const isManual = position.osm_id && position.osm_id.startsWith('manual/');
            const markerColor = isManual ? 'red' : 'blue';
            
            // Create custom icon based on transformer type
            const customIcon = L.divIcon({
                className: 'custom-marker',
                html: `<div style="
                    width: 12px; 
                    height: 12px; 
                    background-color: ${markerColor}; 
                    border: 2px solid white; 
                    border-radius: 50%; 
                    box-shadow: 0 2px 4px rgba(0,0,0,0.3);
                "></div>`,
                iconSize: [16, 16],
                iconAnchor: [8, 8]
            });
            
            const marker = L.marker([lat, lng], { icon: customIcon }).addTo(transformerLayer);
            
            // Store osmId in marker options for deletion
            marker.options.osmId = position.osm_id;
            
            const popupContent = `
                <div class="transformer-popup">
                    <h4>Transformer ${position.osm_id} ${isManual ? '<span style="color: #dc3545; font-size: 12px;">(Manual)</span>' : ''}</h4>
                    <p><strong>OSM ID:</strong> ${position.osm_id || 'N/A'}</p>
                    <p><strong>Capacity:</strong> <span id="capacity-${position.osm_id}">${position.transformer_rated_power ? position.transformer_rated_power + ' kVA' : 'N/A'}</span></p>
                    <p><strong>Type:</strong> ${position.type || 'N/A'}</p>
                    <p><strong>Geometry Type:</strong> ${position.geom_type || 'N/A'}</p>
                    <p><strong>Shopping:</strong> ${position.within_shopping ? 'Yes' : 'No'}</p>
                    <div style="margin-top: 10px;">
                        <select id="edit-capacity-${position.osm_id}" style="width: 100%; margin-bottom: 5px;">
                            <option value="">Select new capacity...</option>
                        </select>
                        <button class="btn" onclick="updateTransformerCapacity('${position.osm_id}')" style="margin-right: 5px;">Update Capacity</button>
                        <button class="btn btn-danger" onclick="deleteTransformer('${position.osm_id}')">Delete</button>
                    </div>
                </div>
            `;
            
            marker.bindPopup(popupContent);
            
            // Populate capacity dropdown when popup opens
            marker.on('popupopen', function() {
                const select = document.getElementById(`edit-capacity-${position.osm_id}`);
                if (select && select.children.length === 1) { // Only if not already populated
                    // Load capacities and populate dropdown
                    loadTransformerCapacities().then(() => {
                        // Copy options from main capacity select to this popup select
                        const mainSelect = document.getElementById('capacity-select');
                        const popupSelect = document.getElementById(`edit-capacity-${position.osm_id}`);
                        if (mainSelect && popupSelect) {
                            popupSelect.innerHTML = mainSelect.innerHTML;
                        }
                    });
                }
            });
        }

        // Update transformer capacity
        async function updateTransformerCapacity(osmId) {
            const select = document.getElementById(`edit-capacity-${osmId}`);
            const newCapacity = select.value;
            
            if (!newCapacity) {
                showStatus('Please select a capacity', 'error');
                return;
            }
            
            try {
                const response = await fetch('/api/update-transformer-capacity', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ 
                        osm_id: osmId,
                        transformer_rated_power: parseInt(newCapacity)
                    })
                });
                
                const data = await response.json();
                
                if (data.success) {
                    showStatus('Transformer capacity updated successfully', 'success');
                    
                    // Update the capacity display in the popup
                    const capacitySpan = document.getElementById(`capacity-${osmId}`);
                    capacitySpan.textContent = newCapacity + ' kVA';
                    
                    // Reset the select
                    select.value = '';
                } else {
                    showStatus('Error updating capacity: ' + data.error, 'error');
                }
            } catch (error) {
                console.error('Error updating capacity:', error);
                showStatus('Error updating capacity: ' + error.message, 'error');
            }
        }

        // Delete transformer
        async function deleteTransformer(osmId) {
            if (!confirm('Are you sure you want to delete this transformer?')) {
                return;
            }
            
            try {
                const response = await fetch('/api/delete-transformer', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ osm_id: osmId })
                });
                
                const data = await response.json();
                
                if (data.success) {
                    // Remove the specific marker from the map
                    transformerLayer.eachLayer(function(layer) {
                        if (layer.options && layer.options.osmId === osmId) {
                            transformerLayer.removeLayer(layer);
                        }
                    });
                    showStatus('Transformer deleted successfully', 'success');
                } else {
                    showStatus('Error deleting transformer: ' + data.error, 'error');
                }
            } catch (error) {
                console.error('Error deleting transformer:', error);
                showStatus('Error deleting transformer: ' + error.message, 'error');
            }
        }


        // Toggle adding mode
        function toggleAddingMode() {
            console.log('toggleAddingMode called', { currentPLZ, currentPLZDisplay, isAddingMode });
            if (!currentPLZ) {
                showStatus('Please load a PLZ first', 'error');
                return;
            }
            
            isAddingMode = !isAddingMode;
            const button = document.getElementById('toggle-add-mode');
            const status = document.getElementById('add-mode-status');
            
            if (isAddingMode) {
                button.style.background = '#dc3545';
                button.textContent = 'Stop Adding Transformers';
                status.textContent = 'Adding mode: ON - Click on map to add transformers';
                status.style.color = '#28a745';
                // Change cursor to crosshair when adding
                document.getElementById('map').style.cursor = 'crosshair';
            } else {
                button.style.background = '#28a745';
                button.textContent = 'Start Adding Transformers';
                status.textContent = 'Adding mode: OFF';
                status.style.color = '#666';
                // Reset cursor to default when not adding
                document.getElementById('map').style.cursor = '';
            }
        }

        // Clear map
        function clearMap() {
            transformerLayer.clearLayers();
            isAddingMode = false;
            currentPLZ = null;
            currentPLZDisplay = null;
            document.getElementById('plz-input').value = '';
            document.getElementById('capacity-select').value = '';
            
            // Reset PLZ display
            document.getElementById('current-plz-display').textContent = 'No PLZ loaded';
            document.getElementById('current-plz-display').style.background = '#e3f2fd';
            document.getElementById('current-plz-display').style.color = '#1976d2';
            
            // Reset adding mode button
            const button = document.getElementById('toggle-add-mode');
            const status = document.getElementById('add-mode-status');
            button.style.background = '#28a745';
            button.textContent = 'Start Adding Transformers';
            status.textContent = 'Adding mode: OFF';
            status.style.color = '#666';
            // Reset cursor
            document.getElementById('map').style.cursor = '';
        }

        // Show status message
        function showStatus(message, type) {
            const statusDiv = document.getElementById('status');
            statusDiv.innerHTML = `<div class="status ${type}">${message}</div>`;
            setTimeout(() => {
                statusDiv.innerHTML = '';
            }, 5000);
        }

        // Test regex patterns
        // Bulk Management Functions
        let availableCapacities = [];
        let transformerCount = 0;
        
        // Load transformer capacities for bulk management
        async function loadTransformerCapacitiesForBulk() {
            try {
                const response = await fetch('/api/transformer-capacities');
                const data = await response.json();
                if (data.success) {
                    // Extract numeric capacity values from the response
                    availableCapacities = data.capacities.map(cap => {
                        // Extract numeric value from the capacity object
                        return parseInt(cap.value);
                    });
                    populateBulkCapacitySelects();
                }
            } catch (error) {
                console.error('Error loading transformer capacities:', error);
            }
        }
        
        // Populate bulk capacity selects
        function populateBulkCapacitySelects() {
            const bulkSelect = document.getElementById('bulk-capacity-select');
            const percentageContainer = document.getElementById('capacity-percentages');
            
            // Clear existing options
            bulkSelect.innerHTML = '<option value="">Select capacity...</option>';
            percentageContainer.innerHTML = '';
            
            // Add options to uniform select
            availableCapacities.forEach(capacity => {
                const option = document.createElement('option');
                option.value = capacity;
                option.textContent = `${capacity} kVA`;
                bulkSelect.appendChild(option);
            });
            
            // Create percentage sliders for each capacity
            availableCapacities.forEach(capacity => {
                const item = document.createElement('div');
                item.className = 'capacity-percentage-item';
                item.innerHTML = `
                    <label>${capacity} kVA:</label>
                    <div class="slider-container">
                        <div class="slider-fill" style="width: 0%"></div>
                        <div class="slider-thumb" style="left: 0%"></div>
                        <input type="range" class="percentage-slider" 
                               min="0" max="100" value="0" 
                               data-capacity="${capacity}"
                               oninput="updatePercentageValue(this)">
                    </div>
                    <span class="percentage-value">0%</span>
                `;
                percentageContainer.appendChild(item);
                
                // Add mouse event handlers to the custom thumb
                const container = item.querySelector('.slider-container');
                const thumb = item.querySelector('.slider-thumb');
                const slider = item.querySelector('.percentage-slider');
                
                // Make the entire container clickable
                container.addEventListener('click', function(e) {
                    const rect = container.getBoundingClientRect();
                    const x = e.clientX - rect.left;
                    // Account for 8px padding on each side
                    const availableWidth = rect.width - 16;
                    const clickPosition = Math.max(0, Math.min(availableWidth, x - 8));
                    const percentage = Math.max(0, Math.min(100, (clickPosition / availableWidth) * 100));
                    slider.value = Math.round(percentage);
                    updatePercentageValue(slider);
                });
                
                // Make thumb draggable
                let isDragging = false;
                
                thumb.addEventListener('mousedown', function(e) {
                    isDragging = true;
                    e.preventDefault();
                });
                
                document.addEventListener('mousemove', function(e) {
                    if (isDragging) {
                        const rect = container.getBoundingClientRect();
                        const x = e.clientX - rect.left;
                        // Account for 8px padding on each side
                        const availableWidth = rect.width - 16;
                        const dragPosition = Math.max(0, Math.min(availableWidth, x - 8));
                        const percentage = Math.max(0, Math.min(100, (dragPosition / availableWidth) * 100));
                        slider.value = Math.round(percentage);
                        updatePercentageValue(slider);
                    }
                });
                
                document.addEventListener('mouseup', function() {
                    isDragging = false;
                });
            });
        }
        
        // Update percentage value display
        function updatePercentageValue(slider) {
            const value = parseInt(slider.value);
            const valueSpan = slider.parentElement.nextElementSibling;
            const sliderFill = slider.previousElementSibling.previousElementSibling;
            const sliderThumb = slider.previousElementSibling;
            
            // Calculate positions accounting for 8px padding on each side
            const containerWidth = slider.parentElement.offsetWidth - 16; // 16px total padding
            const fillWidth = (value / 100) * containerWidth;
            const thumbPosition = 8 + fillWidth; // 8px left padding + fill width
            
            // Update the visual fill and thumb position
            sliderFill.style.width = fillWidth + 'px';
            sliderThumb.style.left = thumbPosition + 'px';
            valueSpan.textContent = value + '%';
            
            // Calculate current total
            const allSliders = document.querySelectorAll('.percentage-slider');
            let currentTotal = 0;
            allSliders.forEach(s => currentTotal += parseInt(s.value));
            
            // If this slider would make total > 100%, cap it
            if (currentTotal > 100) {
                const maxAllowed = 100 - (currentTotal - value);
                slider.value = maxAllowed;
                const cappedFillWidth = (maxAllowed / 100) * containerWidth;
                const cappedThumbPosition = 8 + cappedFillWidth;
                sliderFill.style.width = cappedFillWidth + 'px';
                sliderThumb.style.left = cappedThumbPosition + 'px';
                valueSpan.textContent = maxAllowed + '%';
            }
            
            // Update other sliders' max values and disabled state
            updateSliderConstraints();
            updateTotalPercentage();
        }
        
        // Update slider constraints based on current total
        function updateSliderConstraints() {
            const allSliders = document.querySelectorAll('.percentage-slider');
            let currentTotal = 0;
            allSliders.forEach(s => currentTotal += parseInt(s.value));
            
            allSliders.forEach(slider => {
                const currentValue = parseInt(slider.value);
                const remaining = 100 - (currentTotal - currentValue);
                
                // Set max value to remaining percentage
                slider.max = Math.max(0, remaining);
                
                // Disable if at max and others are at 100%
                if (currentTotal >= 100 && currentValue < remaining) {
                    slider.disabled = true;
                    slider.style.opacity = '0.5';
                } else {
                    slider.disabled = false;
                    slider.style.opacity = '1';
                }
            });
        }
        
        // Update total percentage
        function updateTotalPercentage() {
            const sliders = document.querySelectorAll('.percentage-slider');
            let total = 0;
            sliders.forEach(slider => {
                total += parseInt(slider.value);
            });
            
            const totalSpan = document.getElementById('total-percentage');
            totalSpan.textContent = `Total: ${total}%`;
            
            // Change color based on total
            if (total === 100) {
                totalSpan.style.color = '#28a745';
            } else if (total > 100) {
                totalSpan.style.color = '#dc3545';
            } else {
                totalSpan.style.color = '#ffc107';
            }
        }
        
        // Reset all percentages to 0
        function resetPercentages() {
            const sliders = document.querySelectorAll('.percentage-slider');
            sliders.forEach(slider => {
                slider.value = 0;
                slider.max = 100;
                slider.disabled = false;
                slider.style.opacity = '1';
                // Update the visual fill and thumb
                const sliderFill = slider.previousElementSibling.previousElementSibling;
                const sliderThumb = slider.previousElementSibling;
                sliderFill.style.width = '0px';
                sliderThumb.style.left = '8px'; // 8px left padding
                updatePercentageValue(slider);
            });
        }
        
        // Apply uniform capacity to all transformers
        async function applyUniformCapacity() {
            if (!currentPLZ) {
                showStatus('Please load a PLZ first', 'error');
                return;
            }
            
            const capacity = document.getElementById('bulk-capacity-select').value;
            if (!capacity) {
                showStatus('Please select a capacity', 'error');
                return;
            }
            
            try {
                const response = await fetch('/api/bulk-update-capacities', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        plz: currentPLZ,
                        distribution_method: 'uniform',
                        transformer_rated_power: parseInt(capacity)
                    })
                });
                
                const data = await response.json();
                if (data.success) {
                    showStatus(`Set all ${transformerCount} transformers to ${capacity} kVA`, 'success');
                    // Reload the data to show changes
                    loadPLZData();
                } else {
                    showStatus('Error: ' + data.error, 'error');
                }
            } catch (error) {
                showStatus('Error applying uniform capacity: ' + error.message, 'error');
            }
        }
        
        // Apply percentage distribution
        async function applyPercentageDistribution() {
            if (!currentPLZ) {
                showStatus('Please load a PLZ first', 'error');
                return;
            }
            
            const sliders = document.querySelectorAll('.percentage-slider');
            const distribution = {};
            let total = 0;
            
            sliders.forEach(slider => {
                const capacity = parseInt(slider.dataset.capacity);
                const percentage = parseInt(slider.value);
                if (percentage > 0) {
                    distribution[capacity] = percentage;
                    total += percentage;
                }
            });
            
            if (total !== 100) {
                showStatus('Total percentage must equal 100%', 'error');
                return;
            }
            
            if (Object.keys(distribution).length === 0) {
                showStatus('Please set at least one capacity percentage', 'error');
                return;
            }
            
            try {
                const response = await fetch('/api/bulk-update-capacities', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        plz: currentPLZ,
                        distribution_method: 'percentage',
                        capacity_distribution: distribution
                    })
                });
                
                const data = await response.json();
                if (data.success) {
                    const distributionText = Object.entries(distribution)
                        .map(([cap, pct]) => `${pct}% ${cap}kVA`)
                        .join(', ');
                    showStatus(`Applied distribution: ${distributionText}`, 'success');
                    // Reload the data to show changes
                    loadPLZData();
                } else {
                    showStatus('Error: ' + data.error, 'error');
                }
            } catch (error) {
                showStatus('Error applying distribution: ' + error.message, 'error');
            }
        }
        
        // Clear all capacities for transformers in the current PLZ
        async function clearAllCapacities() {
            if (!currentPLZ) {
                showStatus('Please load a PLZ first', 'error');
                return;
            }
            
            if (!confirm('Are you sure you want to clear all capacity information for transformers in this PLZ area?')) {
                return;
            }
            
            try {
                const response = await fetch('/api/clear-capacities', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        plz: currentPLZ
                    })
                });
                
                const data = await response.json();
                if (data.success) {
                    showStatus('All capacities cleared successfully!', 'success');
                    // Refresh the map to show updated capacities
                    loadPLZData();
                } else {
                    showStatus('Error: ' + data.error, 'error');
                }
            } catch (error) {
                showStatus('Error: ' + error.message, 'error');
            }
        }
        
        // Update transformer count display
        function updateTransformerCount(count) {
            transformerCount = count;
            document.getElementById('transformer-count').textContent = `Transformers in area: ${count}`;
        }
        
        // Handle distribution method change
        function handleDistributionMethodChange() {
            const uniformPanel = document.getElementById('uniform-distribution');
            const percentagePanel = document.getElementById('percentage-distribution');
            const method = document.querySelector('input[name="distribution-method"]:checked').value;
            
            if (method === 'uniform') {
                uniformPanel.style.display = 'block';
                percentagePanel.style.display = 'none';
            } else {
                uniformPanel.style.display = 'none';
                percentagePanel.style.display = 'block';
            }
        }
        
        // Initialize map when page loads
        document.addEventListener('DOMContentLoaded', function() {
            initMap();
            loadPLZList();
            loadTransformerCapacities();
            loadTransformerCapacitiesForBulk();
            
            // Add event listeners for distribution method
            document.querySelectorAll('input[name="distribution-method"]').forEach(radio => {
                radio.addEventListener('change', handleDistributionMethodChange);
            });
        });
    </script>
</body>
</html>'''

    def run(self, debug: bool = False):
        """Start the Flask development server (blocks until it is stopped)."""
        print("Starting Transformer Map UI...")
        print(f"Open your browser and go to: http://{self.host}:{self.port}")
        print("Press Ctrl+C to stop the server")

        try:
            self.app.run(host=self.host, port=self.port, debug=debug)
        except KeyboardInterrupt:
            print("\n🛑 Server stopped by user")


def find_available_port(start_port: int = DEFAULT_PORT, max_attempts: int = 10, host: str = DEFAULT_HOST) -> int:
    """Return the first port from ``start_port`` on that can be bound on ``host``.

    Args:
        start_port: First port to try.
        max_attempts: Number of consecutive ports to try.
        host: Interface to test the ports on.

    Raises:
        RuntimeError: If none of the ports is free.
    """
    for port in range(start_port, start_port + max_attempts):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((host, port))
                return port
        except OSError:
            continue
    raise RuntimeError(f"No available ports found in range {start_port}-{start_port + max_attempts - 1}")


def cleanup_port_processes(port: int) -> bool:
    """Terminate every process that listens on TCP ``port`` (found with ``lsof``, stopped with ``kill``).

    Only listening sockets are matched, so clients that are connected to ``port`` elsewhere are left
    alone. Called only with ``--auto-cleanup``.

    Args:
        port: TCP port to free.

    Returns:
        True if the port was free or the listeners were signalled, False if ``lsof`` failed or is missing.
    """
    try:
        result = subprocess.run(['lsof', '-t', f'-iTCP:{port}', '-sTCP:LISTEN'],
                                capture_output=True, text=True, timeout=5, check=False)
    except subprocess.TimeoutExpired:
        print(f"⚠ Timeout checking port {port}")
        return False
    except FileNotFoundError:
        print(f"⚠ lsof command not found, cannot check port {port}")
        return False

    pids = result.stdout.split()
    if not pids:
        print(f"✓ Port {port} is available")
        return True

    print(f"🧹 Found {len(pids)} process(es) listening on port {port}, terminating them...")
    for pid in pids:
        try:
            subprocess.run(['kill', pid], check=True, timeout=5)
            print(f"✓ Terminated process {pid}")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            print(f"⚠ Could not terminate process {pid}")
    time.sleep(1)  # give the processes a moment to release the port
    return True


def cleanup_lingering_connections():
    """Open and close one database connection before the server starts (``--cleanup``).

    This checks that the database is reachable; it cannot close connections held by other processes.
    """
    try:
        print("🧹 Checking the database connection...")
        DatabaseClient().close()
        print("✓ Database connection check completed")
        time.sleep(0.5)
    except Exception as e:
        print(f"⚠ Warning: Could not connect to the database: {e}")


def run_transformers_ui(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    debug: bool = False,
    cleanup: bool = False,
    auto_cleanup: bool = False,
):
    """Start the transformer map UI.

    Args:
        host: Interface to bind to (default ``127.0.0.1``, local only).
        port: TCP port; ``0`` picks the first free port from 8080 on.
        debug: Run Flask in debug mode and log API requests.
        cleanup: Check the database connection before starting.
        auto_cleanup: Terminate processes that already listen on ``port`` (opt-in).
    """
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    warnings.warn(DEPRECATION_MESSAGE, DeprecationWarning, stacklevel=2)
    logging.getLogger(__name__).warning(DEPRECATION_MESSAGE)
    if Flask is None:
        print("❌ The deprecated transformer map needs Flask: uv sync --extra legacy-ui "
              "(or use the GridPlanner UI with pylovo-api: uv sync --extra api).")
        sys.exit(1)
    if cleanup:
        cleanup_lingering_connections()

    if port == 0:
        try:
            port = find_available_port(DEFAULT_PORT, host=host)
            print(f"🔍 Auto-detected available port: {port}")
        except RuntimeError as e:
            print(f"❌ {e}")
            return
    elif auto_cleanup:
        cleanup_port_processes(port)

    try:
        ui = TransformerMapUI(host=host, port=port)
        ui.run(debug=debug)
    except Exception as e:
        print(f"❌ Error starting Transformer Map UI: {e}")
        print("💡 Make sure the database is running and accessible.")
        print(f"💡 If port {port} is in use, try --port 0 (first free port) or stop the process that uses it.")
        sys.exit(1)


def _add_ui_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the transformer UI options to ``parser`` (shared with ``pylovo-import transformers-ui``)."""
    parser.add_argument('--host', default=DEFAULT_HOST,
                        help=f'Interface to bind to (default: {DEFAULT_HOST}, local only; '
                             '0.0.0.0 exposes the UI and its write access to the network)')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT,
                        help=f'Port number (default: {DEFAULT_PORT}, 0 for the first free port)')
    parser.add_argument('--debug', action='store_true', help='Enable Flask debug mode and debug logging')
    parser.add_argument('--cleanup', action='store_true',
                        help='Check the database connection before starting')
    parser.add_argument('--auto-cleanup', action='store_true',
                        help='Terminate (kill) any process that already listens on --port before starting '
                             '(uses lsof; off by default)')


def main(argv=None):
    """Entry point for ``python -m pylovo.data_import.transformers_ui``."""
    parser = argparse.ArgumentParser(description='Transformer Map UI (deprecated: use the GridPlanner UI)')
    # Accept the accidental passthrough form
    # `python -m pylovo.data_import.transformers_ui transformers-ui`.
    parser.add_argument('compat_command', nargs='?', choices=['transformers-ui'], help=argparse.SUPPRESS)
    _add_ui_arguments(parser)

    args = parser.parse_args(argv)
    run_transformers_ui(
        host=args.host,
        port=args.port,
        debug=args.debug,
        cleanup=args.cleanup,
        auto_cleanup=args.auto_cleanup,
    )


if __name__ == "__main__":
    main()
