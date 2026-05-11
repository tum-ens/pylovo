import pandapower as pp
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import pandapower.plotting as pplot
import os

# Ensure we can see all columns
pd.set_option('display.max_columns', None)
pd.set_option('display.width', 1000)

import pandapower as pp
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import pandapower.plotting as pplot
import pandapower.topology as ppt
import os
import json

# Ensure we can see all columns
pd.set_option('display.max_columns', None)
pd.set_option('display.width', 1000)

def inspect_grid():
    path = "/home/breveron/git/github/pylovo/validation/data/SWF.json"
    print(f"Loading grid from {path}...")
    net = pp.from_json(path)
    
    print("\n" + "="*50)
    print(" 1. GRID DEFINITION ANALYSIS (PHYSICAL VS LOGICAL)")
    print("="*50)
    
    # 1.1 Physical Connectivity
    # Create a graph and count connected components
    try:
        mg = ppt.create_nxgraph(net, include_lines=True, include_trafo=True, include_switches=True)
        # Note: In pp 2.x/3.x create_nxgraph might differ slightly, but checking standard
        components = list(ppt.connected_components(mg))
        print(f"Physical Connected Components (Islands): {len(components)}")
    except Exception as e:
        print(f"Could not calculate connected components: {e}")

    # 1.2 "NetID" Analysis
    if 'chr_name' in net.bus.columns:
        valid_names = net.bus['chr_name'].dropna()
        
        # Methodology A: Full First Segment
        seg1 = valid_names.str.split('_').str[0]
        print(f"Unique 'NetIDs' (Full Segment 1): {seg1.nunique()}")
        
        # Methodology B: User Script Logic (str[1:4])
        # Only for 7-series and 5-series?
        # User script: net.bus.loc[startswith('7')...].str[1:4]
        
        lv_buses = valid_names[valid_names.str.startswith('7', na=False)]
        lv_ids = lv_buses.str[1:4]
        print(f"Unique LV IDs (Digits 2-4 only): {lv_ids.nunique()}")
        print(f"Sample LV IDs: {lv_ids.unique()[:10]}")
        
        mv_buses = valid_names[valid_names.str.startswith('5', na=False)]
        mv_ids = mv_buses.str[1:4]
        print(f"Unique MV IDs (Digits 2-4 only): {mv_ids.nunique()}")
        
        # Reconcile with Transformer Count
        print(f"Total Transformers: {len(net.trafo)}")
        # Check Trafo HV/LV mapping again
        # Do distinct LV NetIDs connect to the same MV NetID?

    print("\n" + "="*50)
    print(" 2. ELEMENT TYPE CODES (PREFIX ANALYSIS)")
    print("="*50)
    
    # Analyze the prefixes (first 2 digits) of Segment 4 (Branch) and Segment 5 (Element)
    # Filter for standard names (5 segments)
    split_names = valid_names.str.split('_', expand=True)
    if split_names.shape[1] >= 5:
        # Segment 4
        s4 = split_names[3]
        s4_prefix = s4.str[:2]
        print("\nSegment 4 (Branch) Prefixes:")
        print(s4_prefix.value_counts().head(10))
        
        # Segment 5
        s5 = split_names[4]
        s5_prefix = s5.str[:2]
        print("\nSegment 5 (Element) Prefixes:")
        print(s5_prefix.value_counts().head(10))
        
        # Correlate with 'type' column if possible
        # Check Bus 'type' vs Segment 4/5?
        print("\nBus Types:")
        print(net.bus['type'].value_counts())
        
        # Check Line names too?
        if 'chr_name' in net.line.columns:
            l_split = net.line['chr_name'].dropna().str.split('_', expand=True)
            if l_split.shape[1] >= 5:
                 print("\nLine Segment 5 Prefixes:")
                 print(l_split[4].str[:2].value_counts().head())

    print("\n" + "="*50)
    print(" 3. MV vs LV STATISTICS")
    print("="*50)
    
    # Analyze 5-series (MV) and 7-series (LV) separately
    
    def analyze_level(prefix, name):
        subset_bus = net.bus[net.bus['chr_name'].str.startswith(prefix, na=False)]
        subset_line = net.line[net.line['chr_name'].str.startswith(prefix, na=False)]
        subset_load = net.load[net.load['chr_name'].str.startswith(prefix, na=False)]
        subset_sgen = net.sgen[net.sgen['chr_name'].str.startswith(prefix, na=False)]
        
        print(f"\n--- {name} ({prefix}...) ---")
        print(f"Buses: {len(subset_bus)}")
        print(f"Lines: {len(subset_line)}")
        print(f"Loads: {len(subset_load)}")
        print(f"Sgens: {len(subset_sgen)}")
        
        # Average per "Grid" (using User Methodology: Digits 2-4 as ID)
        if not subset_bus.empty:
            # Group by ID
            subset_bus['grid_id'] = subset_bus['chr_name'].str[1:4]
            counts = subset_bus.groupby('grid_id').size()
            print(f"Average Buses per {name} Grid (ID based): {counts.mean():.1f}")
            print(f"Min: {counts.min()}, Max: {counts.max()}")

    analyze_level('5', 'MV')
    analyze_level('7', 'LV')
    
    
    print("\n" + "="*50)
    print(" 4. GEOMETRY FIX & PLOTTING")
    print("="*50)
    
    # Check Geometry format
    if 'geo' in net.bus.columns:
        print("Bus 'geo' column found.")
        sample_geo = net.bus['geo'].dropna().iloc[0]
        print(f"Sample Geo Data: {sample_geo} (Type: {type(sample_geo)})")
        
        # If it's a string, we might need to parse it? Or is it a shapely object?
        # If pandapower needs 'bus_geodata', we can try to populate it manually for the plot
        # bus_geodata has columns 'x', 'y' (and maybe 'coords' for lines)
        
        # Try to parse 'geo' into x,y
        # Look at the format. If it's JSON-like or WKT.
        
    else:
        print("No 'geo' column in bus.")

    # Try to plot again with a patch
    output_dir = "/home/breveron/git/github/pylovo/validation/data/plots"
    os.makedirs(output_dir, exist_ok=True)
    
    # Ring Candidate Plot
    ring_candidates = net.bus[net.bus['chr_name'].str.contains('_003001_', na=False)]
    if not ring_candidates.empty:
        ring_bus = ring_candidates.index[0]
        ring_net_id = net.bus.at[ring_bus, 'chr_name'].split('_')[0]
        # Use full ID for selection to be safe, then filter
        
        # Select subgrid
        buses = net.bus[net.bus['chr_name'].str.startswith(ring_net_id, na=False)].index
        try:
            sub = pp.select_subnet(net, buses=buses)
            
            # PATCH GEOMETRY FOR SUBGRID
            # If 'geo' is available in sub.bus, try to populate sub.bus_geodata
            if 'geo' in sub.bus.columns:
                try:
                    # Assuming geo is dictionary or string repr of dict with x/y?
                    # Or maybe list [x, y]?
                    # Let's check the sample printed earlier to know for sure.
                    # Fallback generic handling:
                    pass 
                except:
                    pass
            
            # Simple plot (if no geodata, it uses generic layout which is fine for topology)
            # The user complained about geometry problems?
            # "I saw you had problems with the geometries... use pandapower 2.X.X or solve..."
            # If standard simple_plot fails, it means it tries to access something missing.
            # generic_coordinates might serve.
             
            ax = pplot.simple_plot(sub, show_plot=False, bus_size=0.7, plot_loads=True, plot_sgens=True)
            plt.title(f"Ring Topo: {ring_net_id}")
            plt.savefig(f"{output_dir}/ring_{ring_net_id}.png")
            print(f"Saved {output_dir}/ring_{ring_net_id}.png")
            plt.close()
            
        except Exception as e:
            print(f"Plotting failed: {e}")

if __name__ == "__main__":
    inspect_grid()

