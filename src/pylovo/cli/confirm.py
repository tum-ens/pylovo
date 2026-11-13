"""Target check and typed confirmation in front of the commands that delete data (``pylovo-delete``,
``pylovo-mv delete`` and ``pylovo-setup reset``)."""
import sys

from pylovo import config_loader

# The .env wins over the environment (override=True). If it replaced one of these, the shell and the .env named
# different databases and the command cannot know which one was meant. DBUSER and PASSWORD do not change that.
_TARGET_VARIABLES = ("DBNAME", "HOST", "PORT")


def target_is_unambiguous() -> bool:
    """Check that the ``.env`` did not replace the database name, host or port of the environment.

    This matters for ``HOST=... PORT=... pylovo-delete`` typed in a checkout, and for API jobs after the ``.env``
    changed while the server was running. Prints one line that starts with ``✗`` if the check fails.

    Returns:
        False if ``DBNAME``, ``HOST`` or ``PORT`` of the environment was replaced by the ``.env``, so the
        command must stop and change nothing.
    """
    replaced = [name for name in config_loader.ENV_FILE_REPLACED if name in _TARGET_VARIABLES]
    if not replaced:
        return True
    print(f"✗ The .env file replaced {', '.join(replaced)} of the environment, so the target database is unclear. "
          "Nothing was changed: unset these variables, or set PYLOVO_ENV_FILE= (empty) to use the environment "
          "instead of the .env file")
    return False


def show_target(dbname: str, host: str, port: str) -> None:
    """Print the database a command works on, for the runs where ``--yes`` skips the question."""
    print(f"Target: database {dbname} on {host}:{port}")


def confirm_deletion(what: list[str], dbname: str, host: str, port: str) -> bool:
    """Show what is deleted and where, and ask the user to type the database name.

    The commands skip this question with ``--yes``. Every outcome other than a confirmation prints one line that
    starts with ``✗``.

    Args:
        what: What the command deletes, one line each.
        dbname: Name of the database the command works on (``DBNAME`` of ``.env``).
        host: Database host, shown to the user.
        port: Database port, shown to the user.

    Returns:
        True only if stdin is interactive and the user typed exactly ``dbname``. A mismatch, end of input (Ctrl-D)
        or a non-interactive stdin return False.
    """
    if sys.stdin is None or not sys.stdin.isatty():
        print("✗ stdin is not interactive, nothing was deleted: add --yes to delete without the question")
        return False
    print(f"This deletes data of the database {dbname} (host {host}, port {port}):")
    for line in what:
        print(f"  - {line}")
    try:
        answer = input(f"Type the database name '{dbname}' to continue: ")
    except (EOFError, KeyboardInterrupt):
        print()
        answer = None
    if answer is None or answer.strip() != dbname:
        print("✗ Aborted, nothing was deleted")
        return False
    return True
