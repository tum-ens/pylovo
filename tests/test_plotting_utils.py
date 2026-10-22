"""Colours of the data sources in the real-vs-synthetic comparison plots."""
from pylovo.plotting.utils import COLOR_MAP, get_color_map


def test_color_map_names_only_generic_sources():
    assert set(COLOR_MAP) == {"Real", "Synthetic v1", "Synthetic v2"}


def test_sources_containing_real_get_the_real_colour():
    colors = get_color_map(["Real", "Real (DSO)", "Real grids", "Synthetic v1", "Synthetic v2"])
    assert colors["Real"] == colors["Real (DSO)"] == colors["Real grids"] == COLOR_MAP["Real"]
    assert colors["Synthetic v1"] == COLOR_MAP["Synthetic v1"]
    assert colors["Synthetic v2"] == COLOR_MAP["Synthetic v2"]
