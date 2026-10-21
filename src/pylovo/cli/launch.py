"""Console-script entry points of the ``pylovo-*`` commands that need the configuration.

Each entry first parses the command line with the parser of :mod:`pylovo.cli.parsers`, which imports nothing of
pylovo. ``--help`` and usage errors therefore work without a ``.env`` and without configuration files. Only then
the command module is imported, which loads :mod:`pylovo.config_loader`, and run; it parses the arguments again.
``pylovo-mv`` and ``pylovo-api`` parse before they import the configuration themselves.
"""
import importlib
from collections.abc import Callable

from pylovo.cli import parsers


def _launch(parser: Callable, module: str) -> None:
    arguments = parser()
    if getattr(arguments.parse_args(), "command", "") is None:   # pylovo-delete and pylovo-import: the help
        arguments.print_help()
        return
    importlib.import_module(module).main()


def setup() -> None:
    """Entry point of ``pylovo-setup``."""
    _launch(parsers.setup_parser, "pylovo.cli.setup")


def generate() -> None:
    """Entry point of ``pylovo-generate``."""
    _launch(parsers.generate_parser, "pylovo.cli.generate")


def classify() -> None:
    """Entry point of ``pylovo-classify``."""
    _launch(parsers.classify_parser, "pylovo.cli.classify")


def analyze() -> None:
    """Entry point of ``pylovo-analyze``."""
    _launch(parsers.analyze_parser, "pylovo.cli.analyze")


def delete() -> None:
    """Entry point of ``pylovo-delete``."""
    _launch(parsers.delete_parser, "pylovo.cli.delete")


def export() -> None:
    """Entry point of ``pylovo-export``."""
    _launch(parsers.export_parser, "pylovo.cli.export")


def import_data() -> None:
    """Entry point of ``pylovo-import``."""
    _launch(parsers.import_parser, "pylovo.cli.import_data")
