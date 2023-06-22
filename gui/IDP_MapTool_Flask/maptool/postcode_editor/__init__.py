from flask import Blueprint

bp = Blueprint('postcode_editor', __name__)

from maptool.postcode_editor import routes