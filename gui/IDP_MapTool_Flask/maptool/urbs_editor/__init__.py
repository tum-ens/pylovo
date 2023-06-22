from flask import Blueprint

bp = Blueprint('urbs_editor', __name__)

from maptool.urbs_editor import routes