# Visualization Reset Brief

This file is the starting point for any future work on line visualization.

## Current code state

- Recent generator-side line-visualization logic has been removed again.
- The variable CRS work stays in place and is not part of the visualization reset.
- The `parallel` column stays in place in `lines_result` and `lines_result_with_grid`.
- The default exported GIS geometry is again one geometry per real line segment.

## Fixed decisions

- A segment with `parallel > 1` must still be shown as one line in the default visualization.
- The number of electrical cables is communicated by the `parallel` attribute, not by drawing multiple shifted copies.
- `lines_result` must remain a table of real installed segments.
- Split nodes are part of the actual topology and must be respected by any future visualization.
- Future visualization work should keep code changes as small as possible.

## Important clarification about `parallel`

- `parallel` is currently an electrical sizing result.
- It is increased when one cable is not sufficient for current and voltage-drop limits.
- It is not created just because a route contains a split node.
- Therefore, `parallel = 1` does not mean a line is visually unimportant. It only means one cable is electrically sufficient.

## Preferred direction for future work

- Keep the default GIS segment layer simple: one segment row, one geometry, one `parallel` value.
- If users need better interpretability, build that in a separate postprocessing layer.
- A future helper layer may combine downstream segments so clicking a feature reveals a full cable or feeder extent.
- Any helper geometry should be derived after generation, not by adding more generator-side shift logic.

## Constraints for a future agent

- Do not reintroduce shifted parallel copies into the default segment layer unless explicitly requested.
- Do not mix helper visualization rows into `lines_result`.
- Do not undo the CRS-related changes.
- Before adding new visualization logic, first verify that the result cannot be achieved by a lightweight derived view or helper table.

## Good first step for the next iteration

- Design a small postprocessing helper layer for cable or feeder extent visualization while leaving `lines_result` unchanged.
