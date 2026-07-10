"""
Delete operations for pylovo.
"""
import argparse
import sys

from pylovo.database.database_client import DatabaseClient


def delete_networks(plz: int, version_id: str):
    """Delete networks for a specific PLZ and version."""
    with DatabaseClient() as dbc_client:
        dbc_client.delete_plz_from_all_tables(plz, version_id)
    print(f"✓ Deleted networks for PLZ {plz}, version {version_id}")


def delete_versions(version_ids: list[str]):
    """Delete all networks for one or more versions across all PLZ."""
    with DatabaseClient() as dbc_client:
        deleted_count = dbc_client.delete_versions_from_all_tables(version_ids=version_ids)

    if len(version_ids) == 1:
        print(f"✓ Deleted all networks for version {version_ids[0]}")
    else:
        versions = ", ".join(version_ids)
        print(f"✓ Deleted {deleted_count} versions: {versions}")


def delete_version(version_id: str):
    """Delete all networks for a version across all PLZ."""
    delete_versions([version_id])


def delete_transformers():
    """Delete all transformers."""
    with DatabaseClient() as dbc_client:
        dbc_client.delete_transformers()
    print("✓ Deleted all transformers")


def delete_classification_version(classification_version: str):
    """Delete classification version data."""
    with DatabaseClient() as dbc_client:
        dbc_client.delete_classification_version_from_related_tables(classification_version)
    print(f"✓ Deleted classification version {classification_version}")


def main():
    """Main entry point with subcommands for different delete operations."""
    parser = argparse.ArgumentParser(
        prog="pylovo-delete",
        description="Delete various pylovo data from database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Delete all networks for one version across all PLZ
  pylovo-delete --version 1

  # Delete all networks for multiple versions across all PLZ
  pylovo-delete --version 1 2 3

  # Delete networks for a specific PLZ and version
  pylovo-delete networks --plz 80803 --version 1

  # Delete all transformers
  pylovo-delete transformers

  # Delete classification version data
  pylovo-delete classification --version 1

For more information, see: https://github.com/tum-ens/pylovo
        """
    )
    parser.add_argument(
        "--version",
        dest="versions",
        nargs="+",
        metavar="VERSION_ID",
        help="Version ID(s) to delete across all PLZ (e.g., 1 2 3)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Delete operation to perform", required=False)

    # Subcommand: networks
    networks_parser = subparsers.add_parser(
        "networks",
        help="Delete networks for a specific PLZ and version",
        description="Delete all generated grid networks for a specific postal code and version ID"
    )
    networks_parser.add_argument("--plz", type=int, required=True,
                                help="Postal code (e.g., 80803)")
    networks_parser.add_argument("--version", type=str, required=True,
                                help="Version ID (e.g., 1)")

    # Subcommand: version
    version_parser = subparsers.add_parser(
        "version",
        help="Delete all networks for a version across all PLZ",
        description="Delete all generated grid networks for a specific version across all postal codes"
    )
    version_parser.add_argument("--version", dest="version_ids", type=str, nargs="+", required=True,
                               help="Version ID(s) to delete (e.g., 1 2 3)")

    # Subcommand: transformers
    subparsers.add_parser(
        "transformers",
        help="Delete all transformers",
        description="Delete all transformer data"
    )

    # Subcommand: classification
    class_parser = subparsers.add_parser(
        "classification",
        help="Delete classification version data",
        description="Delete all data associated with a specific classification version"
    )
    class_parser.add_argument("--version", type=str, required=True,
                             help="Classification version (e.g., v1.0)")

    args = parser.parse_args()

    if args.versions and args.command:
        parser.error("--version cannot be combined with a delete subcommand")

    if not args.command and not args.versions:
        parser.print_help()
        return

    try:
        if args.versions:
            delete_versions(args.versions)
        elif args.command == "networks":
            delete_networks(args.plz, args.version)
        elif args.command == "version":
            delete_versions(args.version_ids)
        elif args.command == "transformers":
            delete_transformers()
        elif args.command == "classification":
            delete_classification_version(args.version)
    except ValueError as e:
        print(f"✗ Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
