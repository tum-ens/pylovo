from setuptools import setup
from os import path

here = path.abspath(path.dirname(__file__))

# Get the long description from the README file
with open(path.join(here, "README.md"), encoding="utf-8") as f:
    long_description = f.read()

setup(
    name="syngrid",
    packages=[
        "syngrid",
    ],
    version="0.0.1",
    description="A package for generating syntetic distribution grids",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="TUM ENS",
    author_email="deniz.tepe@tum.de",
    classifiers=[
        "Programming Language :: Python :: 3.9",
    ],
    python_requires=">=3.6",
    install_requires=[
        "pandas",
        "numpy",
        "sqlalchemy<2", # As long as pandas is not compatible with sqlalchemy 2.0, This option seems to be safest.
        "psycopg2",
        "scipy",
        "pandapower",
        "matplotlib",
        "plotly",
        "geopandas",
        "contextily"
    ],
    extras_require={"dev": ["gdal"]},
)
