"""Example figures made with pylovo's own plotting functions.

Requires a pylovo database with the demo region PLZ 85653 and the grid versions
created by ``docs/scripts/make_demo_versions.py``; ``plot_contextily`` downloads
OpenStreetMap tiles and therefore needs internet access. Run from the repository
root (``config/config_generation.yaml`` must have ``VERSION_ID: "1"``)::

    uv run python docs/scripts/plot_pylovo_examples.py

Output: ``docs/source/images/plotting/*.png``.
"""
from __future__ import annotations

import _figure_style as st
import contextily.tile
import matplotlib.pyplot as plt
import pandapower.topology as top

from pylovo.database.database_client import DatabaseClient
from pylovo.plotting.generation.networks import (
    draw_tree_network_spacing,
    plot_contextily,
)

PLZ = 85653
SUBDIR = "plotting"


#: The OpenStreetMap tile servers reject contextily's anonymous default User-Agent
#: (tile usage policy); identify the client for the handful of tiles needed here.
contextily.tile.USER_AGENT = "pylovo-docs/0.7 (+https://github.com/tum-ens/pylovo)"


def main() -> None:
    fig = plot_contextily(plz=PLZ, kcid=1, bcid=4, zoomfactor=17, figsize=(7, 6))
    fig.axes[0].set_title("plot_contextily(plz=85653, kcid=1, bcid=4)", fontsize=10, loc="left")
    fig.text(0.01, 0.01, "Basemap © OpenStreetMap contributors", fontsize=6.5, color=st.INK_SECONDARY)
    st.save(fig, SUBDIR, "plot_contextily")

    with DatabaseClient() as dbc:
        net = dbc.read_net_db(PLZ, 1, 2, version_id="docs_km")
    graph = top.create_nxgraph(net)
    draw_tree_network_spacing(graph)
    fig = plt.gcf()
    fig.set_size_inches(12, 5.5)
    fig.axes[0].set_title("draw_tree_network_spacing(G) for grid kcid 1 / bcid 2 (version docs_km)", loc="left")
    st.save(fig, SUBDIR, "draw_tree_network_spacing", dpi=110)
    plt.close("all")

if __name__ == "__main__":
    main()
