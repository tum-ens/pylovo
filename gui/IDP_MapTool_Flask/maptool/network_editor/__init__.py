from flask import Blueprint

bp = Blueprint('network_editor', __name__)

from maptool.network_editor import routes