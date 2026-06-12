from pathlib import Path

from pylovo.analysis.validation_helpers import split_to_subgrids

# CONFIGURATION
INPUT_FILE = "SWF_3.json"
OUTPUT_DIR = "subnets"

def main():
    print(f"Loading {INPUT_FILE}...")
    results = split_to_subgrids(Path(INPUT_FILE), Path(OUTPUT_DIR), clear_output_dir=True)
    mv_path = results["mv_grid"]
    logical_paths = results["logical_grids"]
    radialized_paths = results["radialized_grids"]
    manifest_path = results["manifest"]

    if mv_path is not None:
        print(f"Saved {mv_path}")
    print(f"Saved {len(logical_paths)} logical LV subnets to {OUTPUT_DIR}/logical")
    print(f"Saved {len(radialized_paths)} radialized LV subnets to {OUTPUT_DIR}/radialized")
    print(f"Saved manifest to {manifest_path}")
    print("Done.")


if __name__ == "__main__":
    main()
