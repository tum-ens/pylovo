"""Delete generated grids, transformers or classification data (``pylovo-delete``)."""
import sys
import traceback

from pylovo.cli import parsers
from pylovo.cli.confirm import confirm_deletion
from pylovo.config_loader import DBNAME, HOST, PORT
from pylovo.database.database_client import DatabaseClient


def delete_networks(plz: int, version_id: str):
    """Delete the grids and results of one PLZ for one version."""
    with DatabaseClient() as dbc_client:
        dbc_client.delete_plz_from_all_tables(plz, version_id)
    print(f"✓ Deleted networks for PLZ {plz}, version {version_id}")


def delete_versions(version_ids: list[str]):
    """Delete the grids and results of one or more versions across all PLZ."""
    with DatabaseClient() as dbc_client:
        deleted_count = dbc_client.delete_versions_from_all_tables(version_ids=version_ids)

    if len(version_ids) == 1:
        print(f"✓ Deleted all networks for version {version_ids[0]}")
    else:
        versions = ", ".join(version_ids)
        print(f"✓ Deleted {deleted_count} versions: {versions}")


def delete_transformers():
    """Delete all rows of ``pylovo.transformers``."""
    with DatabaseClient() as dbc_client:
        dbc_client.delete_transformers()
    print("✓ Deleted all transformers")


def delete_classification_version(classification_id: int):
    """Delete a classification version and its rows in the related classification tables."""
    with DatabaseClient() as dbc_client:
        dbc_client.delete_classification_version_from_related_tables(classification_id)
    print(f"✓ Deleted classification version {classification_id}")


def _scope(args) -> list[str]:
    """What the command of ``args`` deletes, as shown in the confirmation."""
    if args.command == "networks":
        return [f"the grids and results of PLZ {args.plz} in version {args.version}, and the MV variants of that "
                "version that contain the PLZ"]
    if args.command is None:
        return [f"all grids and results in every PLZ, the equipment data, the parameter snapshot and the MV "
                f"variants of version {', '.join(args.versions)}"]
    if args.command == "transformers":
        return ["ALL rows of pylovo.transformers, including the manual and DSO positions, which cannot be recreated "
                "from the OSM files (generated grids keep their positions but lose the link to the transformer)"]
    return [f"classification {args.version} with its sample set and results"]


def main():
    """Entry point of ``pylovo-delete``."""
    parser = parsers.delete_parser()
    args = parser.parse_args()

    if args.versions and args.command:
        parser.error("--version cannot be combined with a delete subcommand")
    if not args.command and not args.versions:
        parser.print_help()
        return
    if not args.yes and not confirm_deletion(_scope(args), DBNAME, HOST, PORT):
        sys.exit(1)

    try:
        if args.versions:
            delete_versions(args.versions)
        elif args.command == "networks":
            delete_networks(args.plz, args.version)
        elif args.command == "transformers":
            delete_transformers()
        elif args.command == "classification":
            delete_classification_version(args.version)
    except ValueError as e:
        print(f"✗ Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"✗ Error: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
