# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

project = 'pylovo gui'
copyright = '2023, Daniel Baur'
author = 'Daniel Baur'
release = '2023'

import os
import sys

sys.path.insert(0, os.path.abspath('../../gui/IDP_Maptool_Flask'))
sys.path.insert(0, os.path.abspath('../..'))
sys.path.insert(0, os.path.abspath('..'))
# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = ['sphinx.ext.autodoc', 'sphinx.ext.coverage', 'autoapi.extension', 'sphinx_js']

autoapi_dirs = ['../../gui/IDP_MapTool_Flask/maptool']

js_source_path = "../../gui/IDP_MapTool_Flask/maptool/static/postcode_editor"

templates_path = ['_templates']
exclude_patterns = []



# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = 'sphinx_rtd_theme'
html_static_path = ['_static']
