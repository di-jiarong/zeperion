"""CLI entry point for ZEPERION.

Supports a shorthand: ``zeperion "implement X"`` is equivalent to
``zeperion run "implement X"``. If the first positional argument is
not a known subcommand, we implicitly prepend ``run``.
"""

import sys


def _maybe_inject_run() -> None:
    """Prepend ``run`` when the user typed ``zeperion "some requirement"``."""
    # Known subcommands (keep in sync with @app.command registrations).
    _COMMANDS = {
        "init", "doctor", "verify", "changes", "discard", "accept",
        "run", "resume", "ship", "status", "list", "logs", "stop", "serve",
        "update", "version",
    }
    # Find the first positional arg (skip leading --flags).
    args = sys.argv[1:]
    for arg in args:
        if arg.startswith("-"):
            continue
        if arg not in _COMMANDS:
            # First positional is not a command → treat as inline requirement.
            sys.argv.insert(1, "run")
        break


def main() -> None:
    """Entrypoint for console_scripts and ``python -m zeperion``."""
    _maybe_inject_run()
    from zeperion.cli import app
    app()


if __name__ == "__main__":
    main()
