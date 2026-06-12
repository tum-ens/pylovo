# DSO Grid Preprocessing for Synthetic-Grid Validation

This note documents how the SWF pandapower model is transformed before it is compared to PyLoVo synthetic LV grids. The goal is not to overwrite the DSO truth, but to create a transparent comparison projection whose assumptions are explicit.

## Source Model

`SWF.json` is a combined MV/LV pandapower model. It contains MV lines and buses, LV grids, MV/LV transformers, switches, loads, generators, and DSO-specific naming metadata in `chr_name`. PyLoVo synthetic grids model LV grids only, so the DSO model must be split before comparison.

## Output Layout

The splitter writes two LV output directories below the selected output root:

- `logical/`: DSO-logical LV subnets grouped by LV `chr_name` subnet ID. This is the audit layer. It keeps the DSO logical grid identity and does not force radial topology.
- `radialized/`: comparison projection of the logical grids. Open-switch lines and cycle-closing lines are marked out of service so that active topology is radial.

A `split_manifest.csv` are written next to those directories. The manifest records every exported or skipped grid, the variant, category, LV-load status, line/load counts, and radialization changes.

File names encode the key assumptions:

```text
LV_041__logical__regular__lvload.xlsx
LV_041__radialized__regular__lvload.xlsx
LV_001__logical__mini__no_lvload.xlsx
```

`regular` means the logical LV subnet has at least `MINI_GRID_BUS_THRESHOLD` buses. `mini` means it is below that threshold. `lvload` means at least one LV comparison load marker was found: `load.name` contains `NS_Last`/`NS-Last`, `load.file` contains that marker, or at least one load has type `HH`. `no_lvload` means none of those markers was found.

## Why Two Variants Are Needed

The logical DSO subnet and the synthetic comparison subnet answer different questions.

The logical subnet preserves the DSO model structure: real line rows, real cycles/rings, switch records, loads, generators, and one copied out-of-service transformer for metadata. This is useful for inspection and for explaining what the DSO data actually contains.

The radialized subnet answers a narrower validation question: how does a PyLoVo radial synthetic LV grid compare to a radial projection of the real DSO LV grid? This avoids comparing synthetic trees against DSO ring/cycle structures without documenting the transformation.

## Transformation Steps

1. Identify LV buses by `bus.chr_name` prefix `7` and subnet ID `chr_name[1:4]`. This is more robust than exact voltage checks because LV bus voltages in SWF can be stored as values such as `0.400000006`.
2. Group logical LV grids by this subnet ID. Cross-subnet LV lines are not part of the logical subnet export.
3. Attach one feeder representation to each exported LV subnet: an `ext_grid` at the transformer LV bus and a copied transformer row marked `in_service=False`. The transformer row preserves capacity and DSO metadata without adding a topology edge to LV distance/feeder metrics.
4. Classify each logical grid as `regular` or `mini` based on bus count.
5. Classify each logical grid as `lvload` or `no_lvload`. A grid is `lvload` when `load.name` contains `NS_Last`/`NS-Last`, `load.file` contains that marker, or at least one load has type `HH`. This excludes medium-voltage-only load grids from the comparison projection while keeping them visible in `logical/` and in the manifest.
6. For radialized grids, mark explicitly open-switch lines out of service.
7. For remaining cycles, keep a length-weighted minimum spanning tree within the transformer-rooted component and mark the other active lines out of service with `split_removed_reason = cycle_radialization`.

## Comparison Input

The comparison workflow should use only:

```text
radialized/LV_*__radialized__regular__lvload.xlsx
```

This keeps the benchmark aligned with PyLoVo's scope: regular LV grids with low-voltage loads and radial topology. The logical outputs remain available for traceability and sensitivity analysis.

## Important Limitations

Radialization is a modeling projection, not a claim that the DSO asset topology is physically radial. The DSO model contains closed cycles/rings in many LV subnets, so removed cycle lines should be interpreted as comparison-only deactivation.

The current radialization rule uses a length-weighted spanning tree. This is simple and reproducible, but it may not match real operational switching decisions. If the DSO provides authoritative normal-open points, those should replace the spanning-tree approximation.

The LV-load filter follows the agreed comparison scope. The manifest records `has_ns_load_name`, `has_ns_load_file`, and `has_hh_load` so the filter can be audited, because not every household-like load is marked consistently in `load.name`.
