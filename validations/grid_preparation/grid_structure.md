# Grid Structure Documentation

This document describes the structure, nomenclature, and data model of the SWF pandapower grid located in `SWF.json`. It has been verified against the actual data using `inspect_grid.py`.

## Overview

The model is a meshed Medium Voltage (MV) and Low Voltage (LV) distribution grid.

- **Total Buses**: 33,657
- **Physical "Islands"**: Each MV/LV Transformer typically feeds one distinct LV grid.
- **Transformers**: 188 total in `net.trafo`:
  - **186 in-service**: 185 LV trafos (LV-side bus `chr_name` prefix `7`, transformer `chr_name` prefix `6`) + 1 HV/MV transformer (`HSMS_UW`, `chr_name` prefix `4`)
  - **2 out-of-service** (`in_service=False`/`FALSCH`): `MSNS_TrSt_0000` (LV_075, 3-bus mini-grid, `chr_name=6075075_...`) and `MSNS_TrSt_0205` (no LV subnet, missing `chr_name`)

Note: LV bus voltage values are stored as floating point values such as `0.400000006`, so code should not identify LV transformers by exact equality to `vn_kv == 0.4`. The `chr_name` prefixes are the more robust classifier in this dataset.

## Nomenclature (`chr_name`)

Most components (`bus`, `line`, `trafo`, `load`, `sgen`) possess a `chr_name` attribute encoded with topological information.

**Format**: `[NetID+Prefix]_[MainNode1]_[MainNode2]_[BranchID]_[ElementID]`
Example: `5001001_001001_001001_062064_01187`

### Segment Breakdown

| Segment | Meaning | Length | Analysis/Code |
| :--- | :--- | :--- | :--- |
| **1. Net Identifier** | Grid ID | 7-12 chars | **Prefix:** `5`=MV, `7`=LV, `6`=Trafo.<br>**ID:** Digits 2-4 (e.g. `169` in `7169169`) represent the specific Subnet ID. |
| **2. MainNode1** | Start Node | 6 digits | Topological anchor. |
| **3. MainNode2** | End Node | 6 digits | Topological anchor. |
| **4. Branch ID** | Strand ID | 6-8 digits | **Prefix Codes**: <br>`00`: Main Feeder/Backbone<br>`06`: Line Branch? (Occurs frequently)<br>`02`: Sub-branch? |
| **5. Element ID** | Element Idx | 5 digits | **Prefix Codes**: <br>`01`: Bus/Node<br>`06`: Line Segment<br>`03`: Load/Gen? |

Transformer LV-side buses are usually classified as follows: `<Net Identifier>_001001_000000_001001_02001`.

*Note: The prefixes in Segments 4/5 are consistent indicators of element type or hierarchy position.*

### Example Decoding
`7169169_001001_001001_003001_01001`
- **7169169**: LV Grid #169.
- **003001**: Branch Code `00` (Main backbone/feeder).
- **01001**: Element Code `01` (Bus).

### Special Cases & Topology
- **Ring Networks**: Indicated by Main Node sequence or specific Branch IDs (e.g., `_003001_`). Verified in Net `7169` (Plot: `plots/ring_7169169.png`).
- **Double Feeding**: **None found at transformer level**. Each LV grid is normally assigned to a single MV/LV transformer. This should not be confused with graph-radial topology: a direct graph recount of the LV line tables found many LV subnets with cycles/ring structures.
- **Geometry**: Available in `bus['geo']` as JSON strings (e.g., `{"coordinates": [x, y], "type": "Point"}`).

## Statistics by Voltage Level

### Medium Voltage (MV)
- **Identifier**: `5xxxxxx`
- **Unique Grid IDs**: 1 (Main Backbone `5001`)
- **Buses**: 13,723
- **Lines**: 13,793
- **Loads**: 0 (Direct loads on MV are rare or modeled as LV aggregated)

### Low Voltage (LV)
- **Identifier**: `7xxxxxx`
- **Unique Grid IDs**: 186 LV subnet IDs from bus `chr_name` prefix `7`; 185 have active LV transformers, and one belongs to an out-of-service LV transformer.
- **Regular Grids**: 85 subnets with ≥ 5 buses (exported to `regular_nets/LV_*.xlsx`)
- **Mini Grids**: 101 subnets with < 5 buses (exported to `mini_grids/`)
- **Average Size**: ~102 Buses per Grid
- **Min/Max**: 1 Bus (stub) to 1,173 Buses.
- **Internal LV Lines**: 19,850 active line rows connect two buses in the same LV subnet; 19,655 remain after excluding lines with an explicitly open line switch.
- **Topology**: Among the 85 regular LV grids, a graph recount found 77 with at least one cycle after excluding lines with explicitly open line switches. This supports the ring-network note above and means the LV asset topology is not purely radial, even though transformer assignment is single-source.
- **Loads**: ~39,800
- **Sgens**: ~15,000

## Data Quality & Anomalies (New Insights)

### 1. Outdated Geodata Format
The original `SWF.json` contained geometry in a non-standard `geo` column containing JSON strings. 
- **Buses**: Had valid Point coordinates.
- **Lines**: **Had NO geometry**. 
- **Fix**: In `SWF_3.json`, line geometries were synthesized from the coordinates of their endpoint buses, and both tables (`bus_geodata`, `line_geodata`) were correctly populated.

### 2. Mini Grids (< 5 Buses)
Analysis revealed **101** of the 186 LV grids have fewer than 5 buses. Of these, 100 have in-service trafos and 1 has an out-of-service trafo (LV_075, MSNS_TrSt_0000).

**Bus count distribution:**
| Buses | Subnets | Description |
|-------|---------|-------------|
| 1     | 75      | 47 empty stubs (no loads) + 28 dedicated industrial consumer trafos |
| 2     | 5       | 4 empty, 1 industrial |
| 3     | 16      | 5 empty, 11 with loads (HH, Ladestation, WP, GHD, Ind) |
| 4     | 5       | 1 empty, 4 with loads |

**Nature:**
- **47 empty 1-bus nodes**: Isolated measurement points, disconnected stubs, or data artifacts (no loads, no sgen).
- **28 single-bus industrial consumers**: Dedicated transformers feeding a single industrial load (type "Ind"), ranging from 27 kW to 5,864 kW. Many exceed their 630 kVA trafo rating (future expansion scenario with `Baujahr > 0` loads).
- **Multi-bus mini grids (2-4 buses)**: Mix of residential (HH), EV charging (Ladestation), heat pumps (WP), commercial (GHD), and industrial (Ind) loads.

**Transformer capacities:** Predominantly 630 kVA (84 of 101), with some 400 kVA (12), 315 kVA (3), 510 kVA (1), and 800 kVA (1).

- **Action**: Segregated into `mini_grids/` during splitting; excluded from comparison metrics but included in geopackage visualization (with `grid_category = "mini_grid"` attribute).

### 3. Crossover Lines (Tie-Lines)
An earlier inspection reported approximately **75 lines** that connect two different Subnet IDs (e.g., Grid `069` to Grid `156`). A newer direct endpoint-based recount on `SWF.json` did not reproduce that exact number:
- 706 active LV-LV line rows connect buses whose `chr_name` subnet IDs differ.
- These rows span 164 unordered LV subnet pairs.
- 173 of those active crossover rows are associated with an explicitly open line switch in `net.switch`; 533 are not flagged by that simple open-switch test.

This discrepancy needs follow-up before using a single crossover-line count as authoritative.

- **Characteristics**: 
    - They are standard line rows connecting buses from different logical LV subnet IDs.
    - Some act as open tie-lines, but the latest simple recount does not support the statement that all or most are explicitly open in `net.switch`.
    - **Nomenclature**: They usually carry the name of *one* of the subnets (e.g., `7069...` connecting to `7156...`).
- **Implication**: If filtering strictly by `chr_name` to split subnets, these lines are often excluded because their endpoint buses belong to different logical groups.
- **Resolution**: These lines are neglected in the radial subnet split to ensure electrical isolation. The electrical status interpretation should be verified against the switch model before treating all neglected crossover rows as open switches.

### 4. Transformer Identification
Transformers are reliably identified via `chr_name`:
- **Trafo `chr_name`**: Prefix `6` (e.g., `6019019_000000_000000_000000_05019`).
- **LV trafo bus `chr_name`**: Prefix `7`, always in the form `7XXXYYY_001001_000000_001001_02001` where `XXX` is the 3-digit subnet ID.
- **MV trafo bus `chr_name`**: Prefix `5` (MV side of the transformer).
- **Authoritative trafo source**: The MV subnet file (`MV_5001.xlsx`) contains all 188 trafos. Each LV subnet file (`LV_XXX.xlsx`) has an `ext_grid` entry at the trafo LV bus but no `trafo` sheet. Using both sources for transformer features causes double-counting (188 + 85 = 273 instead of 186).

## Data Model Attributes
- **Construction Year (`Baujahr`)**: `0` (Existing), `2030+` (Future).
- **Expansion**: Future loads/gens are included in the dataset but marked with `Baujahr > 0`.

## Points Needing Verification
- The crossover-line count and switch-state interpretation should be reconciled between the earlier `~75` statement and the newer endpoint-based recount (`706` active LV-LV cross-subnet rows, `164` unordered subnet pairs).
- The subnet splitting workflow should document exactly whether it excludes all cross-subnet rows, only rows with open line switches, or another filtered subset.
- The distinction between "single transformer source" and "radial graph topology" should be preserved in future validation scripts; the DSO LV grids can be single-source while still containing ring/cycle structures.

## File Information
- **Source**: `validation/data/SWF.json`
- **Scripts**: 
    - `swf_subnets.py`: Utility to split subnets (Patched to filter NaNs).
    - `inspect_grid.py`: Analysis tool used to generate these stats and plots.
