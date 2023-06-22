from flask import Blueprint

bp = Blueprint('urbs_results', __name__)

from maptool.urbs_results import routes