# Version
import numpy as np
import pandas as pd

# Change version if you changed any grid parameters or queries (e.g. transformer query)
VERSION_ID = "1.0"

# state here which parameters, queries are used to compute the grids with current VERSION_ID
VERSION_COMMENT = "base version"


PLOT_COLOR_DICT = {
    100: "gold",
    160: "orange",
    200: "darkorange",
    250: "peru",
    315: "orangered",
    400: "red",
    515: "crimson",
    500: "crimson",
    630: "purple",
    715: "blue",
    800: "darkblue",
    1260: "black",
}
CONNECTION_AVAILABLE_CABLES = [
    "NAYY 4x120 SE",
    "NAYY 4x95 SE",
    "NAYY 4x50 SE",
    "NYY 4x16 SE",
    "NYY 4x35 SE",
    "NYY 4x70 SE",
]

# PARAMETERS
CABLE_COST_DICT = {
    "NAYY 4x185 SE": 33,
    "NAYY 4x150 SE": 24,
    "NAYY 4x120 SE": 19,
    "NAYY 4x95 SE": 16,
    "NAYY 4x50 SE": 11,
    "NYY 4x16 SE": 7,
    "NYY 4x35 SE": 16,
    "NYY 4x95 SE": 37,
    "NYY 4x70 SE": 28,
}

# installed_power * (sim_factor + (1 - sim_factor) * (load_count ** (-3 / 4)))
SIM_FACTOR = {"Residential": 0.07, "Public": 0.6, "Commercial": 0.5}

PEAK_LOAD_HOUSEHOLD = 30.00

CONSUMER_CATEGORIES = pd.DataFrame(
    data=[
        (1, "Commercial", np.nan, np.nan, 79.00, 245.70, 0.50),
        (2, "Public", np.nan, np.nan, 29.00, 155.70, 0.60),
        (9, "SFH", PEAK_LOAD_HOUSEHOLD, 0.00, np.nan, np.nan, 0.07),
        (10, "MFH", PEAK_LOAD_HOUSEHOLD, 0.00, np.nan, np.nan, 0.07),
        (11, "TH", PEAK_LOAD_HOUSEHOLD, 0.00, np.nan, np.nan, 0.07),
        (12, "AB", PEAK_LOAD_HOUSEHOLD, 0.00, np.nan, np.nan, 0.07),
    ],
    columns=[
        "id",
        "definition",
        "peak_load",
        "yearly_consumption",
        "peak_load_per_m2",
        "yearly_consumption_per_m2",
        "sim_factor",
    ],
)

# Larger components are divided by k-means clustering in roughly LARGE_COMPONENT_DIVIDER sizes
LARGE_COMPONENT_LOWER_BOUND = 2000
LARGE_COMPONENT_DIVIDER = 1000

# Voltage properties
VN = 400  # V
V_BAND_LOW = 0.95  # +-5%
V_BAND_HIGH = 1.05


def changeVersionID(version_id):
    VERSION_ID = version_id
