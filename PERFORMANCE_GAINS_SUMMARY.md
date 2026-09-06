# Pylovo performance results

## Outcome

The focused implementation reduces the median end-to-end runtime for PLZ 91301
from **349.67 seconds to 218.73 seconds**. This is a **130.94-second (37.4%)**
runtime reduction and increases single-process throughput from approximately
10.3 to 16.5 PLZ/hour.

All three final candidate versions match deterministic control version 2041 in
the complete semantic database comparison. Peak RSS is unchanged at roughly
1.09 GB.

## Repeated benchmark

All runs used `uv run pylovo-generate --plz 91301 --no-parallel`, inline GIS
visualization, inline PLZ analysis, and identical configuration except for the
version identifier/comment.

| Stage | Version | Wall time | Peak RSS | Semantic comparison |
|---|---:|---:|---:|---|
| Deterministic control | 2041 | 356.15 s | 1,088,956 KB | reference |
| Deterministic control | 2042 | 349.67 s | 1,088,888 KB | pass vs 2041 |
| Deterministic control | 2043 | 347.32 s | 1,088,868 KB | pass vs 2041 |
| **Control median** | | **349.67 s** | **1,088,888 KB** | self-consistent |
| Final candidate | 2061 | 217.57 s | 1,088,984 KB | pass vs 2041 |
| Final candidate | 2062 | 218.73 s | 1,088,496 KB | pass vs 2041 |
| Final candidate | 2063 | 221.09 s | 1,088,468 KB | pass vs 2041 |
| **Candidate median** | | **218.73 s** | **1,088,496 KB** | all pass |

A post-trim verification using version 2064 completed in 220.25 seconds with
1,088,440 KB peak RSS and also passed against version 2041. This confirms that
removing the experimental timing wrappers did not change output or materially
change performance.

The exact final tree was verified once more as version 2065. It completed in
221.95 seconds with 1,088,428 KB peak RSS and passed the complete semantic
comparison against control 2041.

## Why routing reuse is the major lever

The original hotspot performed 12,732 scalar `get_path_to_bus` calls and used
129.799 seconds. The final measured candidates perform one many-to-one query per
grid plus only the correctness fallback pairs:

| Version | Batch calls | Scalar fallbacks | Batch + fallback time |
|---:|---:|---:|---:|
| 2061 | 154 | 144 | 3.525 s |
| 2062 | 154 | 144 | 3.522 s |
| 2063 | 154 | 144 | 3.596 s |

Scalar calls fell by 98.9%, and measured path-query time fell by approximately
97.3%. The cache is a plain dictionary scoped to one grid; there is no cache
service, persistent table, schema change, or configuration switch.

The first prototype, version 2051, was intentionally rejected by the semantic
gate. It reused the reverse batch query for the existing topology cost/order
mapping as well as for paths. Although every cached path matched its scalar
equivalent, that changed tie/insertion ordering used during feeder grouping and
therefore changed grids. Version 2052 fixed this by preserving the established
transformer-to-target query exactly and using the many-to-one query only to
replace repeated scalar path retrieval. Versions 2052 and 2061-2065 all pass.

An exhaustive database validation additionally compared every batch path with
its scalar equivalent for stored version 2043: all **21,880 paths across 154
grids** matched node-for-node and in the same order.

## Keep/revert decisions

Kept:

- Grid-scoped many-to-one pgRouting path reuse with scalar fallback.
- PLZ-scoped node-coordinate and consumer-connection reads.
- PLZ-scoped category/cable metadata reads.
- One batched `lines_result` flush per grid.
- The minimum deterministic input ordering needed for repeatable comparison.
- Standalone semantic comparison, route validation, and historical log-summary
  tools with focused tests.

Reverted:

- Deferred GIS reconstruction, its configuration switch, reconstruction
  helpers, documentation, and analysis CLI path. It moved only about 3.1
  seconds of work and did not remove it.
- Normalized pandapower `execute_values` batching. Its observed benefit was
  about 2.3 seconds (0.6%), which did not justify the extra persistence path.
- Production timing accumulators and per-method/per-grid wrappers. They were
  useful for attribution but are not part of the final generation path.

The earlier v202 batching experiment reduced end-to-end time by 20.125 seconds
(5.5%) and its individually timed operations by 21.952 seconds. Those changes
are localized and remain useful, especially for reducing database round trips
and contention. Routing reuse supplies the material additional gain.

## Verification performed

- Three deterministic controls produced identical semantic output.
- Three final candidates each passed the complete database semantic comparison
  against control 2041.
- Post-trim version 2064 also passed against control 2041.
- Exact-final-tree version 2065 also passed against control 2041.
- All 21,880 stored routing paths passed scalar-vs-batch node-sequence checks.
- `uv run python -m compileall -q src scripts tests`: passed.
- `uv run pytest tests/unit -q`: 13 passed.
- `uv run pytest -m integration -q`: 1 passed, 13 deselected.
- All full generation runs exited with status 0.

The recurring single-grid power-flow non-convergence recorded in each run is
also present in the controls and is represented identically in the persisted
status; it is not a regression from this optimization.

## Remaining next step

State-scale concurrency tuning remains a separate operational benchmark. Test
a fixed representative PLZ set at 4, 6, 8, and 10 workers while monitoring
total throughput, memory, PostgreSQL load, lock waits, I/O, and failures. Do not
change the production worker default from the current six workers until that
sweep identifies a stable higher-throughput setting.
