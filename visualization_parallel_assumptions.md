# Visualization Reset Assumptions

This note restarts the visualization discussion with the revised assumptions.

## Main goals

- Show all electrically parallel cables as separate visible lines whenever the electrical model defines them that way.
- Make it possible to click a line in GIS and understand the full cable extent, not only the next segment up to the next split.
- Avoid visual inconsistencies caused by shifted geometries by design, not by repeated local fixes.
- Keep implementation changes as small as possible and avoid growing the core generation code unnecessarily.
- Respect the newer split-based topology instead of trying to restore the old pre-split behavior.

## Current assumptions

- `lines_result` should remain the table for real installed line segments.
- Visualization-specific helper data should be kept separate from `lines_result`. #Prefer a helper table such as `lines_helper` and expose the combined output through a joined materialized view.#
- The `parallel` attribute is the intended indicator for electrically parallel cables and should be available in the GIS-facing output.
- All persisted geodata should remain in the configured target CRS.
- Any visualization method must work with split nodes as part of the design, not as an exception.

## Open points to audit

- It is still not clear enough to users why pylovo sometimes represents multiple cables with `parallel > 1` and sometimes not. The current code audit shows that this is a sizing result, not a topology result: the code increases `count` only when one cable is not sufficient for current and voltage-drop limits.
- It is still open whether shared first downstream segments should be shown as shared visual lanes or whether a separate postprocessing layer should aggregate them differently.
- It is still open whether `lines_result_with_grid` alone is the right GIS layer for user-facing interpretation.

## Audit result on `parallel`

- `parallel` is currently decided in the cable sizing step, mainly in `CableInstaller.find_minimal_available_cable()` for feeder lines and in the analogous sizing loop for consumer connection cables.
- The algorithm starts with `count = 1` and only increases it when no single cable satisfies the electrical limits.
- Therefore, `parallel > 1` means: multiple electrical cables are required in parallel for that line object.
- Therefore, `parallel = 1` does not mean: the line is not important for visualization. It only means one cable is electrically sufficient.
- Split nodes do not create `parallel` by themselves. They only change how line objects are routed and segmented.
- The `parallel` value is persisted into both the electrical result (`pandapower_line.parallel`) and the GIS segment result (`lines_result.parallel`, exposed through `lines_result_with_grid`).

## Reconsidered assumptions

- ### Visual offsets must not be the primary mechanism for explaining topology to users.
- ### The user-facing layer should not stop at the next connection-cable intersection if the intent is to understand one cable's downstream reach.
- ### It may be better to build a postprocessed feeder or cable-extent layer that combines segments across splits for visualization purposes.
- ### If visual shifts are kept, they must be deterministic and topology-safe so neighboring lines cannot appear disconnected or inconsistent.
- ### A design that requires many invasive changes in the generation code is not acceptable if the same result can be achieved by lighter postprocessing.

## Design direction

- Keep core electrical and segment results clean.
- Add visualization logic in a postprocessing step whenever possible.
- Prefer deriving a dedicated GIS layer for cable interpretation over encoding more visualization behavior directly into line generation.
- Use the electrical model as the source of truth first, then derive readable geometry from it.

## Minimal reset implementation sketch

- Keep `lines_result` unchanged as the source for real installed segments.
- Keep `parallel` on `lines_result` and use it only as an electrical attribute, not as a proxy for visual grouping.
- Add a separate helper structure for visualization. #Preferred direction: a `lines_helper` table plus a joined materialized view.#
- Build the helper data in postprocessing from `lines_result`, `pandapower_line`, and the split topology.
- In that helper layer, assign stable visual lane indices per electrical cable group instead of recomputing shifts during generation.
- Add a second derived layer for cable extent or feeder extent, so clicking one feature can show the whole downstream cable path across multiple split segments.
- Keep the first reset version minimal: derive helper geometries after generation, do not add more branching logic to core cable installation.
- Once the postprocessed layer is accepted, remove as much generator-side visualization logic as possible.