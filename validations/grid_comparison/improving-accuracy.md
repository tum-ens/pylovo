# Improving Grid Accuracy: Attempt Log

This document records the main methodological and parameter attempts used during the validation-oriented calibration of the synthetic PLZ 91301 grids against the radialized DSO benchmark grids. The purpose is to avoid repeating the same experiments without learning from their boundary conditions, and to keep visible which levers remain open.

The active benchmark metrics are feeder-only metrics unless stated otherwise: `feeder_lines`, `graph_length`, `avg_trafo_distance`, `max_trafo_distance`, `transformer_mva`, and `graph_resistance`. Service/consumer connection lines are excluded from length, distance, and resistance metrics because the DSO model uses HaAn-style aggregation while the open-data synthetic model starts from individual buildings.

## Numbered Attempt Log

1. **Separate service-line-inclusive from feeder-only comparison metrics**

   We changed the validation metric calculation to focus on feeder/backbone lines rather than including terminal consumer service lines. This was required because real DSO grids often aggregate several buildings behind HaAn nodes, while synthetic grids from open building data contain individual building service connections. This made the comparison methodologically cleaner and reduced misleading deviations in length, resistance, and distance metrics caused by different service-line modelling detail.

2. **Exclude service connections from graph length, transformer distance, and resistance**

   The six benchmark metrics were adjusted so line-length and resistance proxies measure feeder/backbone structure only. This was especially important after realizing that real and synthetic consumer endpoints are not equally granular. The change improved interpretability, but it did not solve the feeder-count gap because feeder count is still driven by backbone/topology abstraction.

3. **Use terminal-backbone feeder counting instead of raw topology feeder counting**

   The active `feeder_lines` metric was changed to a terminal-backbone definition. The raw topology count remains available in the audit output, but the benchmark metric now aims to count planning-relevant feeder branches while ignoring terminal service connections. This made the metric more comparable, though real and synthetic feeder counts remain derived from different sources: generated branch structure for synthetic grids and topology/label inference for DSO grids.

4. **Handle negative real line lengths by fixing source interpretation and using absolute length defensively**

   Negative line lengths in real DSO data were investigated as a data/model issue. The metric calculation was made robust by using absolute length where needed, but this was not treated as a calibration lever for the synthetic method. It was mainly a validation hygiene fix.

5. **Use known DSO HaAn/load aggregation as a controlled validation scenario**

   We tested aggregating synthetic loads onto known DSO HaAn/connection nodes instead of open-data building points. This clearly improved several metrics and confirmed that load-point abstraction is a major source of mismatch. However, this is not a pure open-data method, so it is useful as an upper-bound/control case rather than the base methodology.

6. **Use known DSO transformer positions and capacities**

   We tested synthetic generation with DSO transformer positions and capacities. This did not automatically improve all metrics; in some runs it worsened representativeness because the assignment logic and load-point abstraction still mattered. The lesson is that correct transformer locations help only if consumer assignment and transformer supply areas are modelled consistently.

7. **Spatially balanced brownfield transformer assignment**

   A balanced assignment idea was tested to avoid poor consumer allocation around known transformers. It worsened many metrics in the tested run and was reverted. The likely reason is that enforcing spatial balance can counteract real supply-area irregularity and does not directly solve open-data load-point granularity.

8. **Voronoi-based transformer assignment/control areas**

   A Voronoi approach around known transformer positions was tested as a methodological alternative for brownfield assignment. It failed for the goal of improving metric accuracy and was reverted. The method was too geometric and not sufficiently aligned with routed LV network structure and load constraints.

9. **Remove oversized service cable option / improve service cable cost selection**

   Service cable selection was adjusted after discovering that cable choice affected resistance metrics. Cost-aware selection helped avoid always choosing technically oversized or inappropriate cables. This improved the resistance interpretation, but service cables are now excluded from the active feeder-only benchmark, so this lever is less central for the six selected metrics.

10. **Make settlement classification deterministic**

   Settlement structure identification was made deterministic to avoid stochastic drift between calibration runs. This was necessary for reproducible comparison, but it is not itself an accuracy lever. It prevents accidental changes in metrics when only one parameter should have changed.

11. **Tune transformer capacity mapping and feeder split threshold**

   We adjusted transformer mappings and increased `FEEDER_SPLIT_MAX_CURRENT_KA` to reduce over-fragmentation and excessive small transformer clusters. This helped compared with earlier baselines, especially after greenfield cluster merging was introduced, but it did not fully close the feeder-count gap.

12. **Introduce conservative greenfield cluster merging**

   A post-processing merge step was added after load-constrained hierarchical clustering. It merges neighboring underutilized greenfield clusters only if the merged cluster still fits a single standard transformer, especially 400 or 630 kVA, and respects the distance constraint. This was one of the clearest successful methodological improvements because it reduced artificial over-fragmentation from recursive clustering while preserving the load-constrained character of the method.

13. **Disable open/brownfield transformer positions for pure greenfield base case**

   We added explicit controls for DSO and open transformer sources and tested pure greenfield generation. This revealed that open transformer data had influenced earlier “no DSO” runs and compromised clean interpretation. For the pure open-data base case, disabling both sources is important when the goal is to evaluate greenfield methodology alone.

14. **Reduce `MAX_GREENFIELD_TRAFO_DISTANCE`**

   The maximum routed distance from a greenfield transformer to assigned connection points was lowered, for example to 1200 m. This helped constrain overly large supply areas and improved some distance-related metrics, but too strict a value can increase transformer count or fragment clusters. It remains a meaningful calibration lever but not enough alone.

15. **Change `MIN_SHARED_PREFIX_LENGTH_M`**

   The shared-prefix threshold controls whether later branches may reuse an existing upstream split point. We tested changes around this parameter and observed that feeder metrics can change, but not always in the intuitive direction, because the metric counts terminal backbone branches and because changing shared prefixes changes the generated branch tree itself. This lever affects topology shape, but it is not the dominant remaining problem.

16. **Reduce voltage band / adjust voltage-related limits**

   Voltage-band changes were tested as a possible lever for distance and topology. They did not solve the feeder-count problem. This confirmed that the active feeder-count gap is mostly topological/geometric rather than voltage-limit-driven.

17. **Initial nearby connection-point aggregation by updating `connection_point` directly**

   The first open-data HaAn proxy aggregated nearby street-side connection points and overwrote `connection_point`. It produced moderate improvements but was conceptually weak because original and aggregated connection points could no longer be distinguished. It also did not fully affect planning as intended because some parts of the generator still used other node columns.

18. **Introduce `agg_connection_point` as separate planning abstraction**

   We replaced the overwrite approach with a cleaner data model: preserve original `connection_point`, add `agg_connection_point`, and make clustering, transformer selection, branch planning, and load grouping use `COALESCE(agg_connection_point, connection_point)`. This is methodologically better and traceable. However, v115 did not improve metrics compared with v112; it slightly worsened mean normalized Wasserstein from about 0.347 to 0.378. It reduced synthetic median feeder lines from 9 to 8 but not nearly enough.

19. **Connection-point aggregation at 25 m, 35 m, 50 m, and extreme 100 m / max 12**

   Several radii and group sizes were tested. The effect was moderate: point counts decreased, but feeder lines stayed too high. Even v115 with 50 m and max 10 reduced original connection points by only about 24% on average. The remaining aggregated connection points were still numerous and distributed across many routed street arms.

20. **Set `FEEDER_SPLIT_MAX_CURRENT_KA` extremely high as diagnostic (`999`)**

   Version 116 tested whether current-based branch splitting is the main driver of excessive feeder lines. It was not. The synthetic feeder median stayed around 8 to 9, while the real median is 4. Distance and resistance metrics improved somewhat, but feeder count did not. This strongly suggests that the remaining feeder-count gap is driven more by spatial/topological demand-point distribution than by the current cap.

21. **Investigate street-key fragmentation in connection-point aggregation**

   We found that about 70% of v115 load rows had `address_street_id`, about 71% had `street`, and 100% had `assigned_way_id`. The current grouping key was `address_street_id -> assigned_way_id -> street`, meaning about 29% of rows fell back to `assigned_way_id`. If `assigned_way_id` refers to split way segments, this fragments aggregation along the same street. This is a strong candidate for why the aggregation did not compress enough.

22. **Relax the street constraint in connection-point aggregation**

   The aggregation logic was adjusted to stop using `assigned_way_id` as fallback because it can represent fragmented split-way segments. If stable street information is available, grouping still uses `address_street_id -> street`. If no stable street information is available, nearby connection points are grouped geometrically in a shared no-street bucket. Version 117 improved the mean normalized Wasserstein score compared with v115 (about 0.378 to 0.350), but did not outperform the best old aggregation run v112 (about 0.347). Interestingly, the total connection-point compression was weaker than v115, so the improvement likely comes from grouping different points and changing the resulting topology rather than simply grouping more points.

23. **Revisit load-point abstraction beyond local nearest-street aggregation**

   The remaining gap suggests that real DSO HaAn abstraction may aggregate more than nearby same-street points. Second-row buildings, courtyard buildings, and building groups may be represented behind fewer modelled connection points. This has not yet been fully implemented. It is likely one of the remaining high-value methodological levers, but it needs care to avoid inventing unrealistic aggregation.

24. **Revisit feeder-count metric symmetry**

   The real feeder count is inferred from DSO topology and labels, while synthetic feeder count comes from generated branch plans and split points. We improved this by using terminal-backbone feeder counts, but the metric may still not be fully symmetric. Further validation against visual examples could identify whether real KVS/cable-node interpretation is still collapsing more topology than the synthetic metric.

## Success and Retry Rating

Scale: 1 means failure or only diagnostic value; 10 means strong successful accuracy improvement. “Retry worth” reflects whether another run is likely useful under updated boundary conditions.

| # | Attempt | Success rating | Retry worth? | Why |
|---:|---|---:|---|---|
| 1 | Feeder-only comparison metrics | 8 | No | Methodological correction already adopted; not a calibration lever. |
| 2 | Exclude service lines from length/distance/resistance | 8 | No | Correct benchmark assumption; keep as baseline. |
| 3 | Terminal-backbone feeder counting | 7 | Maybe | Better than raw counts, but metric symmetry should still be visually audited. |
| 4 | Negative length handling | 6 | No | Validation hygiene, not accuracy calibration. |
| 5 | Known DSO HaAn/load aggregation | 8 | Yes, as control | Useful upper-bound experiment, not pure open-data base case. |
| 6 | Known DSO transformer positions/capacities | 4 | Maybe | Needs consistent assignment/load abstraction; not automatically beneficial. |
| 7 | Spatially balanced transformer assignment | 2 | No, not now | Worsened metrics and was reverted. |
| 8 | Voronoi transformer assignment | 2 | No | Failed for routed LV topology representativeness. |
| 9 | Service cable catalogue/cost selection | 6 | Low | Important for electrical realism; less relevant for feeder-only metrics. |
| 10 | Deterministic settlement classification | 7 | No | Reproducibility fix; keep. |
| 11 | Transformer mapping and feeder split calibration | 6 | Yes | Still relevant, but not the main feeder-count driver after v116. |
| 12 | Greenfield cluster merging | 9 | Yes | Clear methodological improvement; further fine-tuning may help. |
| 13 | Disable DSO/open transformers for pure greenfield base | 8 | No | Necessary experimental control; keep for base case. |
| 14 | Lower `MAX_GREENFIELD_TRAFO_DISTANCE` | 6 | Yes | Useful for distance metrics and supply-area realism; tune carefully. |
| 15 | Tune `MIN_SHARED_PREFIX_LENGTH_M` | 4 | Maybe | Affects topology but did not clearly solve feeder count. |
| 16 | Voltage band changes | 2 | No | Not the relevant lever for feeder-count accuracy. |
| 17 | Overwrite `connection_point` aggregation | 4 | No | Replaced by cleaner `agg_connection_point` design. |
| 18 | Separate `agg_connection_point` planning abstraction | 5 | Yes | Correct data model, but current grouping logic is too weak. |
| 19 | Radius/max-size aggregation sweeps | 4 | Yes, only with changed grouping key | Repeating same street-key logic is unlikely to help enough. |
| 20 | `FEEDER_SPLIT_MAX_CURRENT_KA = 999` diagnostic | 3 | No | Showed current cap is not main feeder-count driver. |
| 21 | Street-key fragmentation analysis | 8 | Yes | Strong evidence that `assigned_way_id` fallback may block aggregation. |
| 22 | Stable-street or no-street-bucket aggregation | 6 | Maybe | Improved v115 but not v112; useful but not the final lever. |
| 23 | Stronger open-data load-point abstraction | Not run | Yes | Likely important, but needs careful design. |
| 24 | Further real/synthetic feeder metric symmetry audit | Not run | Yes | Needed before over-calibrating method to a possibly asymmetric metric. |

## Current Interpretation

The best current open-data methodological improvement is greenfield cluster merging combined with feeder-only validation metrics. The `agg_connection_point` design is the right data model, but the present aggregation heuristic did not reduce feeder lines enough. Version 116 showed that current-based feeder splitting is not the main remaining cause of too many feeder lines.

The most promising next diagnostics are:

1. If needed, run an extreme no-street-partition aggregation as an upper-bound diagnostic.
2. Audit real and synthetic feeder-count examples visually to confirm the active `feeder_lines` metric is symmetric enough.
3. Audit real and synthetic feeder-count examples visually to confirm the active `feeder_lines` metric is symmetric enough.
4. If aggregation still fails, design a stronger open-data load-point abstraction for building groups rather than only nearby street-side connection points.
