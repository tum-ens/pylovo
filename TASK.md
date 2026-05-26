- First read the AGENTS.md file if not already done.

# Goal
A clear view of the grids cable topology in GIS applications, especially after the newly introduced option for cables to split

## Rules
- Keep code changes as small as possible.
- pandapower `parallel` is currently an electrical sizing result (parallel cables are used when one cable is electrically not sufficient for current and voltage-drop limits). The number of electrical cables is communicated by the `parallel` column attribute in `lines_result`, not by drawing multiple shifted copies. A segment with `parallel > 1` must still be shown as one line in the default visualization.
-  Split nodes are part of the actual topology and must be respected by any future visualization. After splitting points of cables I want to see shifted GIS segments, also when they are using the exact same lane.
- `lines_result` should remain a table of real installed segments.
- Do not mix helper visualization rows into `lines_result`, but create a new table `lines_result_helper`
- Combine `lines_result` and `lines_result_helper` in the `lines_result_with_grid` view
- use geopandas to visualize and anticipate possible graphical problems
- brownfield transformers should be placed equivalent to greenfield transformers at the connection_line intersection of the identified best building node for transformer positioning
