"""Typed confirmation in front of the commands that delete data (``pylovo-delete`` and ``pylovo-mv delete``)."""
import sys


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
