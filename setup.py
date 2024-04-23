from setuptools import setup
from os import path

here = path.abspath(path.dirname(__file__))

# Get the long description from the README file
with open(path.join(here, "README.rst"), encoding="utf-8") as f:
    long_description = f.read()

setup(
    name="pylovo",
    packages=[
        "syngrid",
    ],
    version="1.0.0",
    description="A package for generating syntetic distribution grids",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="TUM ENS",
    author_email="beneharo.reveron-baecker@tum.de",
    classifiers=[
        "Programming Language :: Python :: 3.9",
    ],
    python_requires=">=3.9",
    install_requires=[
        "pandas==2.0.0",
        "numpy==1.24.4",
        "SQLAlchemy==1.4.47",  # As long as pandas is not compatible with sqlalchemy 2.0, This option seems to be safest.
        "psycopg2_binary==2.9.9",
        "scipy==1.10.1",
        "pandapower==2.12.1",
        "matplotlib==3.7.1",
        "plotly==5.14.1",
        "geopandas==0.12.2",
        "seaborn==0.12.2",
        "openpyxl==3.1.1",
        "xlrd==2.0.1",
        "python-dotenv"
    ],
    extras_require={
        "dev": [
            "gdal",
            "mkdocs",
            "mkdocs-material",
            "mkdocstrings-python",
            "mkdocs-autorefs",
            "sphinx-autoapi",
            "sphinx-js",
            "sphinx-rtd-theme==1.2.0"
        ],
        "gui": [
            "Werkzeug==2.2.2",
            "flask==2.2.3",
            "beautifulsoup4==4.11.1",
        ],
        "test": [
            "pytest"
        ]
    },
)
