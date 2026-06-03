# Project Memory

## 2026-05-26 - GIS helper rows for overlapping cable segments

- What was decided: Use postprocessing helper generation for GIS visualization. `lines_result` remains real installed segments only, while `lines_result_helper` stores shifted helper copies for exact duplicate line geometries. `lines_result_with_grid` combines real and helper rows and marks helpers with `is_helper`.
- Why: This keeps electrical sizing and topology persistence clean while making overlapping same-lane segments visible in GIS tools such as QGIS.
- What was rejected and why: Generator-aware visualization logic was rejected because it is more invasive and mixes visualization concerns into cable installation. A schema-only helper table was rejected because it would not improve visibility by itself.

## 2026-05-26 - Brownfield transformer placement uses connection points

- What was decided: Brownfield transformer grid positions use the transformer row's `connection_point` as `ont_vertice_id` and store the geometry from `ways_tem_vertices_pgr`, with comment `on_way`.
- Why: This makes brownfield transformer placement equivalent to greenfield placement at the connection-line/way intersection instead of using the transformer/building center geometry.
- What was rejected and why: Keeping brownfield placement at `buildings_tem.center` was rejected because it produces different GIS topology from greenfield transformer placement.

## 2026-05-29 - Split-topology helpers replace exact-duplicate helper detection

- What was decided: GIS helper rows should be generated from split topology in `branch_plans`, not from exact full-geometry duplicates. A pandapower line element with `parallel > 1` still remains one visual line; helper rows are for separate real backbone line rows created by split topology.
- Why: The intended visual problem is split-generated cables sharing lanes or route prefixes, not only rows with byte-identical full geometries.
- What was rejected and why: Exact full-geometry duplicate detection was rejected because it misses shared-prefix split routes. Brownfield transformer placement changes were also removed because that requirement was reverted from `TASK.md`.

## 2026-05-29 - Reconnect split-topology helpers to real segment endpoints

- What was decided: Build helper geometries by offsetting the real segment and then reattaching both original endpoints, so helper lines reconnect at the same topology nodes as the supplied real segment.
- Why: Pure offset curves can appear visually detached at segment ends in GIS, especially around building-connection splits.
- What was rejected and why: Keeping plain `ST_OffsetCurve` output was rejected because it can leave apparent dangling helper ends in QGIS and produce an unclean graph view.

## 2026-05-29 - Normalize offset-curve direction before helper endpoint reconnect

- What was decided: For split-topology helper generation, align the offset curve direction with the source line direction before adding original endpoints.
- Why: On one offset side (negative distances), PostGIS can return an offset curve with reversed direction; if endpoints are reattached without normalization, long cross-connectors create distorted helper geometries.
- What was rejected and why: Keeping endpoint reconnect without direction normalization was rejected because it produced the reported weird helper line shape (e.g., source line 1165 / helper L2481).

## 2026-05-29 - Suppress helperized source rows in lines_result_with_grid

- What was decided: In `lines_result_with_grid`, exclude real `lines_result` rows that already have a corresponding helper row in `lines_result_helper` (`source_lines_result_id = lines_result_id`).
- Why: This removes duplicate plotting in QGIS and keeps helper offsets as the single visual representation for helperized split-topology segments.
- What was rejected and why: Keeping both source and helper rows in the visualization view was rejected because it double-draws segments and makes interpretation of true parallel-cable situations harder.

## 2026-05-29 - Add split_points table for split-topology visualization

- What was decided: Add `split_points` as a persistent table with point geometries per split node (`split_bus`) and populate it during generation from split-topology nodes.
- Why: This provides an explicit GIS layer to inspect where feeder topology actually splits.
- What was rejected and why: Deriving split points only ad-hoc in QGIS was rejected because it is not reproducible in the generated database output.

## 2026-05-29 - Restrict split_points to feeder split topology only

- What was decided: `split_points` now stores feeder split points only, derived from feeder helper topology (split edges), and excludes house-connection branching points.
- Why: The intended GIS interpretation is split locations at `Kabelmuffen` / `Kabelverteilerstationen`, not service drop branching.
- What was rejected and why: Counting all multi-outgoing `from_bus` nodes from `lines_result` was rejected because it over-represents house-connection branch nodes and clutters the topology view.

## 2026-05-29 - Exclude transformer nodes from split_points

- What was decided: Exclude each grid's `ont_vertice_id` from `split_points`.
- Why: Transformer positions are already represented by dedicated transformer layers; including transformer nodes in split points causes redundant visualization.
- What was rejected and why: Keeping transformer nodes in `split_points` was rejected because it blurs the interpretation of true feeder split locations.

## 2026-05-29 - Add shared-route overlap helpers for missed split visuals

- What was decided: After split-edge helper generation, add a conservative overlap helper pass for remaining feeder lines_result rows that share at least 10 m of exact route geometry and do not already have helper rows. The shorter overlapping feeder row is helperized and marked with helper_type shared_route_overlap_offset.
- Why: Some split-generated topology rows, such as L1832 and L1898, share a lane segment even though they are not sibling edges from the same split parent, so the existing split-edge helper detection misses them.
- What was rejected and why: Splitting real lines_result rows at internal vertices was rejected because it would alter installed topology persistence for a visualization problem. A 1 m overlap threshold was rejected because it affected more existing rows than needed for meaningful lane-segment overlays.

## 2026-06-03 - Store selected generation parameters in version JSONB

- What was decided: Add `pylovo.version.generation_parameters` as a JSONB snapshot populated from selected result-affecting `config_generation.yaml` settings: load calculation, equipment records, cable dimensioning limits, and transformer placement/clustering thresholds.
- Why: These parameters materially affect generated grid results, but their YAML/list structure does not fit cleanly into many scalar table columns for the current need. A single JSONB cell keeps the version row informative while remaining simple to implement and queryable when needed.
- What was rejected and why: Adding many scalar columns was rejected for now because the user chose the simpler JSONB-only approach. Storing all execution settings from `config_generation.yaml` was rejected because parallelism, logging, output folders, and similar runtime controls do not define the grid result itself.

## 2026-06-03 - Remove testing-mode support artifacts

- What was decided: Remove all testing-mode support artifacts by deleting the test postcode import module, removing the `pylovo-import test-postcodes` CLI path, and dropping `allocated_plz` from the fresh `postcode` table schema.
- Why: The project no longer needs compatibility with the `TESTING` generation mode, so keeping test postcode import plumbing and the associated schema column would add dead code and confusing setup surface.
- What was rejected and why: Actively dropping `allocated_plz` from existing databases was rejected for now because the chosen approach only changes the greenfield schema and avoids destructive migration behavior.
