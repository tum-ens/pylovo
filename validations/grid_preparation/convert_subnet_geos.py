from pathlib import Path

from pylovo.analysis.validation_helpers import fix_subnet_geos


DEFAULT_BASE_DIR = Path("/home/breveron/git/github/pylovo/validation/data/subnets")


def main() -> None:
    success_count, fail_count = fix_subnet_geos(DEFAULT_BASE_DIR)
    print(f"Done. Success: {success_count}, Fail: {fail_count}")


if __name__ == "__main__":
    main()
