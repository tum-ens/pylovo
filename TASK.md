- First read the AGENTS.md file if not already done.
- Then read MEMORY.md and ERRORS.md before changing code.

# Task: Uniform Feeder Cable Type Per Hard-Node Section

## Objective

Change backbone / feeder cable sizing so each feeder section between hard nodes uses one uniform cable type and one uniform `parallel` count.

The desired implementation should be as small as possible while still fixing the behavior. Prefer reusing the existing backbone planning and line creation flow over introducing new schema, new persisted segment IDs, or broad abstractions.

## Background

The current generator sizes feeder cables per physical line edge. This can produce a feeder chain where consecutive backbone edges use different cable types, for example:

- `NAYY_4_120`
- `NAYY_4_150`

Each individual edge may be electrically valid, but this does not match the intended planning model for German low-voltage feeder sections. In practice, cable type changes should happen at hard nodes, not at every intermediate node created by house-connection routing or topology segmentation.

## Current Symptom

The GIS-facing `lines_result_with_grid` view merges feeder topology chains between hard nodes. When a merged chain contains several feeder edge cable types, the view exposes this as a concatenated `std_type`, for example:

```text
NAYY_4_120+NAYY_4_150
```

The target outcome is that merged feeder rows have a single feeder cable name in `std_type`, without `+`.

## Existing Code Context

The relevant code is already close to the needed behavior:

- `src/pylovo/grid_generator.py`
  - `_plan_backbone_branches(...)` freezes the branch topology before feeder lines are created.
  - `_install_backbone_lines_two_pass(...)` builds `children_by_node`, calculates downstream load per feeder edge, selects one cable per edge, and calls the existing line creation methods.
  - `_get_split_visualization_edges(...)` derives split topology for GIS helper rows.
- `src/pylovo/cable_installer.py`
  - `find_minimal_available_cable(Imax, distance)` already encapsulates feeder cable selection.
  - `create_line_node_to_node(...)`, `create_line_start_to_lv_bus(...)`, and `create_line_ont_to_lv_bus(...)` already create backend and database line records from an already chosen cable/count.
- `src/pylovo/database/grid_mixin.py` and `src/pylovo/database/config_table_structure.py`
  - `lines_result_with_grid` merges feeder chains and uses `STRING_AGG(DISTINCT fl.std_type, '+') AS std_type`.
  - This view should become clean naturally if the underlying feeder edges in each merged hard-node section share one `std_type`.

## Hard Nodes

For this task, hard nodes are:

- the transformer vertex, `ont_vertice`
- feeder split points generated from the finalized branch topology

Intermediate feeder nodes that exist only because of route geometry or house-connection branching should be treated as pass-through nodes for feeder cable selection.

This should align with the existing `lines_result_with_grid` logic, where merged feeder chains run between transformer/split hard nodes and pass through feeder-degree-2 non-hard nodes.

## Required Behavior

For every feeder section between hard nodes:

1. Identify all feeder edges belonging to that section.
2. Calculate the worst feeder requirement for the whole section.
3. Select one cable type and one `parallel` count that satisfy that worst requirement.
4. Use that same cable/count for every feeder edge in the section.
5. Keep consumer connection cable sizing unchanged.
6. Keep the existing `lines_result` and pandapower line records as edge-level records.
7. Do not add `segment_id` or new database schema unless it becomes strictly necessary.

## Non-Goals

Do not change these unless there is a direct blocker:

- consumer/service connection cable sizing in `install_consumer_cables(...)`
- the electrical backend interfaces
- `lines_result` schema
- `lines_result_with_grid` schema
- GIS helper generation
- transformer placement
- branch planning topology

If one of these appears necessary, stop and explain why before changing it.

## Preferred Minimal Design

Implement segment-level feeder cable selection inside or immediately next to `_install_backbone_lines_two_pass(...)`.

The smallest likely change is:

1. Keep building `children_by_node` and `downstream_nodes_by_node` as today.
2. Derive the same feeder graph edges that `_install_backbone_lines_two_pass(...)` is about to install.
3. Derive hard nodes from:
   - `ont_vertice`
   - nodes with more than one feeder child in `children_by_node`
4. Group feeder edges into chains between hard nodes.
5. For each chain, compute a single `(cable, count)` using the worst edge load and worst relevant routed distance.
6. During the existing installation loops, look up the precomputed `(cable, count)` for the current edge and pass it to the existing `create_line_*` methods.

This keeps line creation, `local_length_dict`, backend insertion, and database insertion largely unchanged.

## Segment Grouping Algorithm

The grouping can be done from the finalized directed feeder tree:

```text
parent node -> child node
```

Build this tree from `branch_plans`, using the same orientation already used in `_install_backbone_lines_two_pass(...)`.

Suggested algorithm:

1. Build `children_by_node`.
2. Define `hard_nodes`:
   - `{ont_vertice}`
   - plus every parent node where `len(children_by_node[parent]) > 1`
3. For each hard node, start one chain for each outgoing child.
4. Walk downstream while the current node is not a hard node and has exactly one child.
5. Stop the chain when:
   - a hard node is reached, or
   - a leaf is reached, or
   - a node has zero or multiple children
6. Store a mapping from each edge `(parent, child)` to a segment key.

Important detail:

- Some final feeder branches may end at leaves rather than another split point. Those should still form one uniform section from the upstream hard node to the leaf.
- If the existing `lines_result_with_grid` view treats a node as a hard node differently, prefer matching that view's logic over inventing a separate definition.

## Segment Cable Selection

For each segment, choose one cable/count based on worst requirements across all edges in that segment.

Use existing logic where possible:

- Continue using `CableInstaller.find_minimal_available_cable(...)`.
- Avoid duplicating cable filtering logic in `grid_generator.py`.

For each edge in a segment:

1. Compute downstream load exactly as current edge-level sizing does:

```python
sim_load = utils.simultaneousPeakLoad(
    buildings_df,
    consumer_df,
    downstream_nodes_by_node[child],
)
Imax = sim_load / (VN * V_BAND_LOW * np.sqrt(3))
```

2. Compute edge distance as current code does:

```python
edge_distance = vertices_dict[child] - vertices_dict[parent]
```

3. For the segment, use:

```python
segment_Imax = max(edge_Imax values)
segment_distance = sum(edge_distance values)
```

4. Select:

```python
cable, count = installer.find_minimal_available_cable(
    segment_Imax,
    segment_distance,
)
```

Then assign that same `cable` and `count` to every feeder edge in the segment.

### Voltage-Drop Distance

Use the total routed length of the hard-node section for `segment_distance`:

```python
segment_distance = sum(edge_distance values)
```

Reason: once cable dimensioning is decided for the combined section, voltage drop must be checked across that full section. Using only the longest individual edge would preserve more of the old per-edge behavior, but it would understate the voltage-drop requirement for the combined feeder section.

## Direct Transformer Edge Note

`create_line_ont_to_lv_bus(...)` creates a backend-only line from `LVbus 1` to a connection node and does not insert a `lines_result` row. Keep its current behavior unless a failing test or GIS symptom directly requires otherwise.

For regular feeder rows that are inserted into `lines_result`, apply the segment-level choice.

## Expected Implementation Shape

Likely small helper functions in `GridGenerator`:

```python
def _build_feeder_edges_from_branch_plans(...):
    ...

def _group_feeder_edges_by_hard_node_section(...):
    ...

def _select_cables_for_feeder_sections(...):
    ...
```

Keep helpers private and local to `grid_generator.py` unless the logic truly belongs in `CableInstaller`.

Avoid adding a new class or persisted segment model for this task.

## Validation

Use `uv run` for all test and validation commands.

At minimum:

1. Run focused tests if existing tests cover grid generation or cable installation.
2. Run a lightweight import or syntax check if full generation is too expensive.
3. If possible, generate or inspect a small/known PLZ where the previous symptom appeared.

Suggested SQL validation after generation:

```sql
SELECT line_name, std_type, kcid, bcid, plz
FROM pylovo.lines_result_with_grid
WHERE helper_type = 'merged_feeder_topology'
  AND std_type LIKE '%+%';
```

Expected result:

```text
0 rows
```

Also inspect underlying feeder rows in a problematic grid:

```sql
SELECT from_bus, to_bus, std_type, parallel, length_km
FROM pylovo.lines_result
WHERE grid_result_id = <grid_result_id>
ORDER BY from_bus, to_bus;
```

Within each hard-node section, `std_type` and `parallel` should be uniform.

## Testing Ideas

Prefer focused tests over broad slow tests.

Useful tests:

1. Segment grouping
   - A simple chain with no split should produce one segment.
   - A transformer with two downstream branches should produce one segment per outgoing branch.
   - A split point with two children should end the upstream segment and start downstream segments.

2. Segment cable choice
   - If one edge in a segment requires `NAYY_4_150`, all edges in that segment should receive `NAYY_4_150`.
   - If one edge requires `parallel = 2`, all edges in that segment should receive `parallel = 2`.

3. Regression check
   - Consumer connection cable selection remains unchanged.
   - `local_length_dict` still counts `parallel * length_km` for each inserted feeder edge.

## Success Criteria

The task is complete when:

- feeder sections between hard nodes use one uniform `std_type`
- feeder sections between hard nodes use one uniform `parallel`
- merged feeder rows in `lines_result_with_grid` no longer show `+` in `std_type`
- consumer/service cable sizing is unchanged
- no unnecessary database schema or backend changes were introduced
- the implementation remains small and easy to follow

## Notes For Future Agent

- Start with `_install_backbone_lines_two_pass(...)`; it is probably the right integration point.
- Do not start by modifying `install_consumer_cables(...)`; that handles service cables and is outside the immediate symptom.
- Do not solve this by changing `STRING_AGG(DISTINCT fl.std_type, '+')` to hide the symptom. The underlying feeder rows should actually be uniform within each merged section.
- Prefer a precomputed edge-to-cable mapping over rewriting the existing line creation methods.
- If you find that hard-node grouping differs between Python and the SQL view, document the difference before changing code.
