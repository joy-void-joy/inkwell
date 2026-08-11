"""Compile the entry point declaration into the typer commands beside this.

Typer reads a command's parameters off a real function signature, which is the
one surface a declaration cannot build at import time without hiding the
signature from the type checker and from ``--help``. So the signature is rendered
as source: one command per entry point in
:data:`inkwell.environment.entrypoints.ENTRY_POINTS`, written to ``commands.py``
and reconciled by the same drift check that keeps the generated harness trees
current. ``inkwell.devtools.harness.composition`` owns that writer; this module
owns only the text.

Every help string, flag, annotation, and default is read off the declaration, so
a parameter added there reaches the command line by regenerating rather than by
an edit. The rendering carries a trailing comma inside every bracket it opens,
which is what holds it in the shape ``ruff format`` produces — the artifact is
source under ``src/``, so it has to be formatted source.
"""

import json
from collections.abc import Iterator

from inkwell.environment.entrypoints import (
    ENTRY_POINTS,
    EntryPoint,
    EntryPointParameter,
)

MODULE_DOCSTRING = [
    '"""One typer command per declared writing entry point.',
    "",
    "Each command's parameters, help text, flags, and defaults come from the entry",
    "point declaration in :mod:`inkwell.environment.entrypoints`; the body collects",
    "them under their declared names and hands them to the one launch path this CLI",
    "and the web API both run through. Adding a parameter to a declaration and",
    "regenerating is the whole of the change here.",
    '"""',
]
"""What the compiled file says about itself, before the first command."""

MODULE_IMPORTS = [
    "from typing import Annotated",
    "",
    "import typer",
]
"""All a compiled command needs at module scope; the launch path is imported
inside each body, so `inkwell --help` does not pay for the pipeline."""


def python_string(text: str) -> str:
    """``text`` as the double-quoted literal ``ruff format`` would leave alone.

    JSON string syntax is a subset of Python's, so its escaping is reused rather
    than hand-rolled; leaving non-ASCII unescaped keeps an em dash in the help an
    em dash in the source.
    """
    return json.dumps(text, ensure_ascii=False)


def invocation_lines(parameter: EntryPointParameter) -> Iterator[str]:
    """The ``typer.Argument``/``typer.Option`` call a signature carries."""
    match parameter.surfaces.cli:
        case "argument":
            yield "        typer.Argument("
        case "option":
            yield "        typer.Option("
            for flag in parameter.flags:
                yield f"            {python_string(flag)},"
        case "root" | "none":
            raise ValueError(
                f"{parameter.name} declares no place on a command, so no command "
                "can spell it"
            )
    yield f"            help={python_string(parameter.help_text)},"
    yield "        ),"


def parameter_lines(parameter: EntryPointParameter) -> Iterator[str]:
    """One parameter of a command signature, annotated and defaulted."""
    yield f"    {parameter.name}: Annotated["
    yield f"        {parameter.cli_annotation},"
    yield from invocation_lines(parameter)
    default = parameter.cli_default
    yield f"    ] = {default}," if default else "    ],"


def docstring_lines(entry_point: EntryPoint) -> Iterator[str]:
    """The command's docstring, which is what ``--help`` prints."""
    if not entry_point.detail:
        yield f'    """{entry_point.summary}"""'
        return
    yield f'    """{entry_point.summary}'
    yield ""
    for line in entry_point.detail.splitlines():
        yield f"    {line}" if line else ""
    yield '    """'


def command_lines(entry_point: EntryPoint) -> Iterator[str]:
    """One entry point as the typer command that starts it."""
    yield f"def {entry_point.name}("
    for parameter in entry_point.command_parameters:
        yield from parameter_lines(parameter)
    yield ") -> None:"
    yield from docstring_lines(entry_point)
    yield "    from inkwell.environment.cli.chat import run_entry_point"
    yield ""
    yield "    run_entry_point("
    yield f"        {python_string(entry_point.name)},"
    yield "        {"
    for parameter in entry_point.command_parameters:
        yield f"            {python_string(parameter.name)}: {parameter.name},"
    yield "        },"
    yield "    )"


def register_lines() -> Iterator[str]:
    """The one function the CLI calls to mount every compiled command."""
    yield "def register(app: typer.Typer) -> None:"
    yield '    """Add every declared entry point to ``app`` as its own command."""'
    for entry_point in ENTRY_POINTS:
        yield f"    app.command({python_string(entry_point.name)})({entry_point.name})"


def module_lines() -> Iterator[str]:
    """The compiled file, docstring through registration."""
    yield from MODULE_DOCSTRING
    yield ""
    yield from MODULE_IMPORTS
    for entry_point in ENTRY_POINTS:
        yield ""
        yield ""
        yield from command_lines(entry_point)
    yield ""
    yield ""
    yield from register_lines()


def render_entry_point_commands() -> str:
    """Every declared entry point, compiled to typer command source."""
    return "\n".join(module_lines()) + "\n"
