from pathlib import Path

from pylovo.analysis.validation_helpers import split_to_subgrids

# CONFIGURATION
INPUT_FILE = "SWF_3.json"
OUTPUT_DIR = "subnets"

def main():
    print(f"Loading {INPUT_FILE}...")
    results = split_to_subgrids(Path(INPUT_FILE), Path(OUTPUT_DIR), clear_output_dir=True)
    mv_path = results["mv_grid"]
    lv_paths = results["lv_grids"]

    if mv_path is not None:
        print(f"Saved {mv_path}")
    print(f"Saved {len(lv_paths)} LV subnets to {OUTPUT_DIR}")
    print("Done.")


if __name__ == "__main__":
    main()
