# Validation Assumptions

This document records the main modelling assumptions used when comparing pylovo synthetic low-voltage grids with the DSO reference grids for PLZ 91301. The goal is to keep the benchmark interpretable and to avoid calibrating pylovo against artefacts of the two different model representations.

## 1. Use radialized DSO grids for topology comparison

The DSO source model contains operational switching states, open switches, and meshed structures that are not directly comparable to pylovo's radial normal-operation grid synthesis. For the benchmark, the radialized DSO exports are used. Explicitly open switch lines and cycle-closing lines are marked out of service, while the logical exports are kept separately for transparency and inspection.

## 2. Compare only LV grids with LV load relevance

Subnets without `NS-Last` / LV-load relevance are excluded from the benchmark. pylovo generates LV supply grids for low-voltage consumers, so medium-voltage-only loads or empty switching stubs would distort the comparison target.

## 3. Preserve transformer metadata without treating MV connections as LV topology

The DSO preprocessing attaches transformer metadata to each LV subnet, but the comparison topology is rooted on the LV side. Transformer ratings are compared through `transformer_mva`; MV-side lines or transformer metadata edges are not treated as LV feeder topology.

## 4. Use terminal feeder branches, not service stubs, for feeder count

The feeder-count metric is intended to describe LV backbone topology. Terminal service-connection stubs to individual house connections are pruned before counting terminal feeder branches. For the SWF/DSO data, service lines are identified mainly through line names such as `NS_KbAn_*` and `NS_FlAn_*`, guarded by endpoint semantics such as `HaAn`, `Kn(n)_KbAn`, `Kn(n)_FlAn`, explicit load buses, and `ErLast`. For synthetic grids, terminal `Consumer Nodebus` leaves are treated as consumer service endpoints.

Terminal service attachments with several final consumer/load leaves are also removed from the backbone graph when they have exactly one non-consumer neighbour. This represents aggregated house-connection points: `backbone -> service attachment -> one or more final consumers` is service-side modelling and is removed, while `backbone -> node -> downstream backbone` is retained as topology-relevant feeder structure.

## 5. Use feeder/backbone metrics by default

The six active benchmark metrics are interpreted as feeder/backbone metrics. `feeder_lines` already counts terminal backbone branches after pruning service endpoints. `graph_length` and `graph_resistance` exclude terminal house/consumer service-connection stubs using the same pruning logic. `avg_trafo_distance` and `max_trafo_distance` are measured to terminal feeder/backbone nodes after service pruning, rather than to individual consumer attachment buses. `transformer_mva` is independent of service-line modelling.

This is necessary because the DSO model aggregates several buildings behind HaAn connection points, while the open-data synthetic model may represent many individual building service connections. Including all service connections would make the metrics sensitive to the modelling abstraction of consumer attachment points rather than to feeder-backbone structure. The validation CLI can still export the old service-line-inclusive length, resistance, and distance metrics with `--with-service-lines` for diagnostics.

## 6. Treat graph resistance as a backbone electrical proxy

`graph_resistance` sums the equivalent resistance of active feeder/backbone line segments, including cable type and parallel conductor effects. Terminal service connections are excluded by default so that the metric is aligned with the active topology-length and transformer-distance assumptions. Service-line-inclusive resistance should only be used as a separate diagnostic because it is strongly affected by HaAn aggregation and building-level service modelling.

## 7. Use absolute line lengths for DSO metrics when source data contain negative values

Some DSO line lengths can appear with negative signs due to source-data conventions or export artefacts. These are not interpreted as physically negative cable lengths. Validation routines should use absolute physical lengths or otherwise normalize them before metric calculation.

## 8. Treat DSO HaAn aggregation as a representational difference, not as open-data input

The real DSO model contains house-connection aggregation that is not generally available from open data. For the pure synthetic baseline, pylovo should not use known HaAn positions. Metrics that are sensitive to this abstraction, especially service-including length or resistance metrics, must be interpreted carefully or designed to focus on backbone topology.

## 9. Separate pure synthetic, open-transformer, and DSO-informed scenarios

Transformer-position evidence is source-dependent. Pure synthetic runs should set both `USE_DSO_TRANSFORMER_POSITIONS` and `USE_OPEN_TRANSFORMER_POSITIONS` to `False`. Open-data transformer experiments may enable only open/manual transformer positions. DSO-informed validation experiments may enable DSO transformer positions, but those runs are not pure open-data baselines.

## 10. Use the full synthetic export for the main score, with status diagnostics retained

The primary Wasserstein score uses the full exported synthetic metric set. Power-flow statuses such as `converged` and `voltage_violation` are retained in diagnostics to understand feasibility, but they are not used to silently remove difficult synthetic grids from the representativeness comparison unless a separate sensitivity analysis states this explicitly.
