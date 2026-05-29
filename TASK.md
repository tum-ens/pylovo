- First read the AGENTS.md file if not already done.

# Goal
- A clear view of the grids cable topology in GIS applications (such as QGIS), especially after the newly introduced option for cables to be able to split

## Rules
- Keep code changes as small as possible.
- use geopandas to visualize and anticipate possible graphical problems
- First understand the clustering, grid generation and cable installation approach in this repository to better understand the description below

## Description
- pandapower `parallel` is currently an electrical sizing result (parallel cables are used when one cable is electrically not sufficient for current and voltage-drop limits). The number of electrical cables is communicated by the `parallel` column attribute in `lines_result`, not by drawing multiple shifted copies. A segment with `parallel > 1` must still be shown as one line in the default visualization.
- Important distinction: do not create shifted GIS copies because one pandapower line element has `parallel > 1`. That `parallel` value only means one physical cable type was electrically insufficient and pandapower models multiple electrical cables on the same installed segment.
- I do want shifted GIS segments when separate real line rows are generated because of cable splitting/topology at split points. These are different installed topology segments, even if they use the same lane or share part of the same lane. They should be visually separable in GIS as parallel-looking lines.
- Split nodes are part of the actual topology and must be respected by any future visualization. After splitting points of cables I want to see shifted GIS segments for the split-generated lines, also when they use the exact same lane or overlap only on a shared route prefix. To visualize this clearly, helper lines may be needed at the splitting point to keep a consistent graph with visible parallel lines.
- Exact full-geometry duplicates are not the intended detection rule. The visualization should identify lines related by split topology/shared route usage, not only rows whose complete geometry bytes are identical.
- `lines_result` should remain a table of real installed segments (no helper lines). Therefore, do not mix helper visualization rows into `lines_result`, but create a new table `lines_result_helper`
- In the end, combine `lines_result` and `lines_result_helper` in the `lines_result_with_grid` view
- Do not draw duplicate geometry in `lines_result_with_grid`: if a helper row exists for a real source line (`source_lines_result_id`), the corresponding source row must be suppressed in the view and only the helper row should be shown.
- `split_points` must represent feeder cable split points only ("Kabelmuffen" / "Kabelverteilerstationen" topology). House-connection branching points must be excluded.
- Transformer nodes (`ont_vertice_id`) must be excluded from `split_points` even when feeder branches split directly at the transformer; transformer locations are already represented in dedicated transformer tables.
