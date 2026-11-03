"""Example figures made with pylovo's own plotting functions, from the example data of the tutorials.

Needs no database and no internet: :class:`pylovo.examples.TutorialData` reads the rural example area from
``data/examples``. Run from the repository root::

    uv run --extra plots python docs/scripts/plot_pylovo_examples.py

Output: ``docs/source/images/plotting/*.png``.
"""
from __future__ import annotations

import _figure_style as st
import pandapower.topology as top

from pylovo.examples import TutorialData
from pylovo.plotting.generation.networks import draw_tree_network
from pylovo.plotting.lv_grid import plot_lv_grid
from pylovo.plotting.voltage_levels import plot_voltage_levels

PLZ = 85307
SUBDIR = "plotting"


def main() -> None:
    data = TutorialData(PLZ, source="examples")
    kcid, bcid = data.typical_grid
    st.save(plot_lv_grid(data.layers, kcid, bcid, color_by="cable", buildings="peak_load"), SUBDIR, "plot_lv_grid",
            dpi=130)

    net = data.net(kcid, bcid)
    st.save(draw_tree_network(top.create_nxgraph(net), net=net, y="distance"), SUBDIR, "draw_tree_network", dpi=120)

    levels = data.voltage_levels(kcid, bcid)
    st.save(plot_voltage_levels(levels["chain"], levels["hv"], levels["mv_context"], levels["mv_grid"], levels["lv"]),
            SUBDIR, "plot_voltage_levels", dpi=110)


if __name__ == "__main__":
    main()
