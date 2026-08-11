"""An entry point's parameters are declared once and rendered onto three surfaces.

The typer command, the API request model, and the browser form used to declare
the same parameters independently, and they had drifted: ``light`` existed on the
request model and reached the pipeline, was absent from the CLI entirely, and was
never sent by the form. These tests pin the arrangement that makes that state
unreachable — one declaration, three renders — rather than pinning the symptom.

What is checked here: every surface's render agrees with the declaration; a
parameter that exists only in a declaration reaches all three renders with no
other edit; every parameter a surface leaves out records why; and the inventory
of what the three surfaces carried before is accounted for, parameter by
parameter.
"""

import ast

import pytest
from fastapi import HTTPException
from rich.text import Text
from typer.testing import CliRunner

import inkwell.environment.cli.compile as compile_module
from inkwell.devtools.harness.composition import write_entry_point_commands
from inkwell.environment.cli.compile import render_entry_point_commands
from inkwell.environment.entrypoints import (
    ENTRY_POINTS,
    LIGHT,
    RESTART,
    RESUME,
    REVISE,
    RUN,
    STAGE_MODELS,
    WRITE,
    EntryPoint,
    EntryPointParameter,
    EntryPointValues,
    FlagParameter,
    SurfacePlan,
    entry_point_descriptors,
    entry_point_named,
    request_model,
)
from inkwell.environment.web.models import (
    CreateSessionRequest,
    RestartSessionRequest,
)
from inkwell.environment.web.routes import sessions
from inkwell.environment.web.session_manager import SessionManager


def rendered_command(entry_point_name: str, source: str = "") -> ast.FunctionDef:
    """The compiled typer command for one entry point, as its parsed signature."""
    module = ast.parse(source or render_entry_point_commands())
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == entry_point_name:
            return node
    raise AssertionError(f"no compiled command named {entry_point_name!r}")


def signature_names(command: ast.FunctionDef) -> list[str]:
    """The parameter names the compiled command's signature spells, in order."""
    return [argument.arg for argument in command.args.args]


def descriptor_names(entry_point: EntryPoint) -> list[str]:
    """The parameter names the browser receives for one entry point."""
    return [p.name for p in entry_point.descriptor().parameters]


def command_help(entry_point_name: str) -> str:
    """What ``inkwell <name> --help`` prints, with typer's styling stripped.

    Rich splits an option name across colour spans, so the raw output holds no
    ``--light`` to look for; the ANSI is parsed back off rather than searched
    around.
    """
    from inkwell.environment.cli.__main__ import app

    result = CliRunner().invoke(
        app, [entry_point_name, "--help"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.output
    return Text.from_ansi(result.output).plain


class TestEachSurfaceRendersTheDeclaration:
    """Every surface's render is exactly what the declaration asked for."""

    @pytest.mark.parametrize("entry_point", ENTRY_POINTS, ids=lambda e: e.name)
    def test_command_signature_is_the_declared_command_parameters(
        self, entry_point: EntryPoint
    ) -> None:
        command = rendered_command(entry_point.name)
        assert signature_names(command) == [
            p.name for p in entry_point.command_parameters
        ]

    @pytest.mark.parametrize("entry_point", ENTRY_POINTS, ids=lambda e: e.name)
    def test_request_model_fields_are_the_declared_api_parameters(
        self, entry_point: EntryPoint
    ) -> None:
        model = request_model(entry_point)
        assert list(model.model_fields) == [p.name for p in entry_point.api_parameters]

    @pytest.mark.parametrize("entry_point", ENTRY_POINTS, ids=lambda e: e.name)
    def test_descriptor_carries_the_declared_form_parameters(
        self, entry_point: EntryPoint
    ) -> None:
        assert descriptor_names(entry_point) == [
            p.name for p in entry_point.form_parameters
        ]

    def test_descriptor_marks_bespoke_controls_as_not_generically_rendered(
        self,
    ) -> None:
        rendered = {p.name: p.rendered for p in WRITE.descriptor().parameters}
        # The source picker and the format description have hand-written UI; the
        # rest of the write form is built from the descriptor.
        assert rendered["sources"] is False
        assert rendered["target_format"] is False
        assert rendered["stop_after"] is True
        assert rendered["existing_doc_id"] is True

    def test_every_command_is_registered(self) -> None:
        source = render_entry_point_commands()
        for entry_point in ENTRY_POINTS:
            assert f'app.command("{entry_point.name}")({entry_point.name})' in source

    def test_the_committed_commands_are_what_the_declaration_renders(self) -> None:
        """The checked-in artifact is drift-checked, like any generated file."""
        write_entry_point_commands(check=True)


class TestADeclarationOnlyParameterReachesAllThree:
    """A parameter added to a declaration and nowhere else reaches every surface.

    This is the property the whole arrangement exists for, so it is exercised
    rather than asserted in prose: the parameter below is declared here, in this
    test, and no renderer has ever heard of it.
    """

    @pytest.fixture
    def declared_only(self) -> EntryPointParameter:
        return FlagParameter(
            name="dry_run",
            label="Dry run",
            help="Plan the piece without writing it",
            flags=["--dry-run"],
        )

    @pytest.fixture
    def extended(self, declared_only: EntryPointParameter) -> EntryPoint:
        return WRITE.model_copy(
            update={"parameters": [*WRITE.parameters, declared_only]}
        )

    def test_it_reaches_the_command_signature(
        self, extended: EntryPoint, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(compile_module, "ENTRY_POINTS", [extended])
        command = rendered_command("write", render_entry_point_commands())
        assert "dry_run" in signature_names(command)
        assert '"--dry-run"' in render_entry_point_commands()

    def test_it_reaches_the_request_model(self, extended: EntryPoint) -> None:
        model = request_model(extended)
        assert "dry_run" in model.model_fields
        body = model.model_validate({"sources": ["x"]})
        assert body.model_dump()["dry_run"] is False

    def test_it_reaches_the_form_descriptor(self, extended: EntryPoint) -> None:
        descriptor = next(
            p for p in extended.descriptor().parameters if p.name == "dry_run"
        )
        assert descriptor.widget == "flag"
        assert descriptor.rendered is True
        assert descriptor.label == "Dry run"

    def test_it_reaches_the_launch_path_without_a_new_argument(
        self, extended: EntryPoint, declared_only: FlagParameter
    ) -> None:
        values = EntryPointValues(entry_point=extended.name, supplied={"dry_run": True})
        assert declared_only.read(values) is True


class TestDriftIsClosed:
    """Every parameter the three surfaces carried is accounted for."""

    def test_light_reaches_every_surface(self) -> None:
        assert LIGHT in WRITE.parameters
        assert "light" in signature_names(rendered_command("write"))
        assert "light" in request_model(WRITE).model_fields
        descriptor = next(p for p in WRITE.descriptor().parameters if p.name == "light")
        assert descriptor.widget == "flag"
        assert descriptor.rendered is True

    def test_light_reaches_the_command_line_help(self) -> None:
        assert "--light" in command_help("write")

    def test_restart_and_revise_have_commands(self) -> None:
        """Restart had no command at all, and revise is new; both are declared."""
        assert "--from" in command_help("restart")
        assert "DRAFT" in command_help("revise")

    @pytest.mark.parametrize(
        ("entry_point", "carried"),
        [
            # What each surface spelled by hand before the declaration existed:
            # the CLI's write/run/resume signatures, CreateSessionRequest,
            # ResumeSessionRequest, RestartSessionRequest, and the form's fields.
            (
                WRITE,
                [
                    "sources",
                    "refs",
                    "target_format",
                    "existing_doc_id",
                    "session_id",
                    "stop_after",
                    "verbose",
                    "light",
                    "profile",
                    "model",
                    "stage_models",
                    "writer_mode",
                ],
            ),
            (
                RUN,
                [
                    "task",
                    "target_format",
                    "session_id",
                    "stop_after",
                    "verbose",
                ],
            ),
            (
                RESUME,
                [
                    "resumed_session",
                    "from_stage",
                    "stop_after",
                    "verbose",
                    "profile",
                    "model",
                    "stage_models",
                    "writer_mode",
                ],
            ),
            (
                RESTART,
                [
                    "resumed_session",
                    "from_stage",
                    "profile",
                    "model",
                    "stage_models",
                    "writer_mode",
                ],
            ),
        ],
        ids=["write", "run", "resume", "restart"],
    )
    def test_the_old_surfaces_inventory_is_declared(
        self, entry_point: EntryPoint, carried: list[str]
    ) -> None:
        declared = {p.name for p in entry_point.parameters}
        assert declared.issuperset(carried)

    def test_revise_feeds_the_ordinary_pipeline_with_its_standing_instruction(
        self,
    ) -> None:
        values = EntryPointValues.declared("revise", {"draft": "draft.md"})
        sources = REVISE.sources(values)
        assert sources[0] == "draft.md"
        assert "clarity and currency" in sources[1]

    def test_a_run_session_writes_from_its_task(self) -> None:
        values = EntryPointValues.declared("run", {"task": "write about X"})
        assert RUN.sources(values) == ["write about X"]


class TestASurfaceMustSayWhyItLeavesAParameterOut:
    """An omission is a decision on the record, not an absence nobody notices."""

    @pytest.mark.parametrize("entry_point", ENTRY_POINTS, ids=lambda e: e.name)
    def test_every_omission_records_its_reason(self, entry_point: EntryPoint) -> None:
        for parameter in entry_point.parameters:
            if parameter.surfaces.absent_somewhere:
                assert parameter.surfaces.omitted_because, parameter.name

    def test_a_silent_omission_is_refused(self) -> None:
        with pytest.raises(ValueError, match="omitted_because"):
            SurfacePlan(api=False)

    def test_a_reason_without_an_omission_is_refused(self) -> None:
        with pytest.raises(ValueError, match="omitted_because"):
            SurfacePlan(omitted_because="no surface leaves this out")


class TestOneCoercionServesEverySurface:
    """A rule declared once holds wherever the value arrived from."""

    def test_a_google_doc_url_reduces_to_its_id(self) -> None:
        values = EntryPointValues.declared(
            "write",
            {
                "sources": ["x"],
                "existing_doc_id": "https://docs.google.com/document/d/abc123/edit",
            },
        )
        assert entry_point_named("write").parameters
        assert values.supplied["existing_doc_id"] == (
            "https://docs.google.com/document/d/abc123/edit"
        )
        from inkwell.environment.entrypoints import EXISTING_DOC_ID

        assert EXISTING_DOC_ID.read(values) == "abc123"

    def test_a_bare_document_id_passes_through(self) -> None:
        from inkwell.environment.entrypoints import EXISTING_DOC_ID

        values = EntryPointValues.declared(
            "write", {"sources": ["x"], "existing_doc_id": "abc123"}
        )
        assert EXISTING_DOC_ID.read(values) == "abc123"

    def test_a_stage_that_names_no_checkpoint_is_refused(self) -> None:
        with pytest.raises(ValueError, match="Invalid stage"):
            EntryPointValues.declared(
                "write", {"sources": ["x"], "stop_after": "nonsense"}
            )

    def test_a_stage_the_pipeline_checkpoints_is_accepted(self) -> None:
        from inkwell.environment.entrypoints import STOP_AFTER

        values = EntryPointValues.declared(
            "write", {"sources": ["x"], "stop_after": "Plan"}
        )
        assert STOP_AFTER.read(values) == "plan"

    def test_the_command_line_and_the_api_agree_on_stage_models(self) -> None:
        from_command = EntryPointValues.declared(
            "write", {"sources": ["x"], "stage_models": ["plan=claude-opus-5"]}
        )
        from_api = EntryPointValues.declared(
            "write", {"sources": ["x"], "stage_models": {"plan": "claude-opus-5"}}
        )
        assert STAGE_MODELS.read(from_command) == STAGE_MODELS.read(from_api)

    def test_a_malformed_stage_model_pair_is_refused(self) -> None:
        with pytest.raises(ValueError, match="stage=model"):
            EntryPointValues.declared(
                "write", {"sources": ["x"], "stage_models": ["plan"]}
            )

    def test_a_stage_nobody_declared_is_refused(self) -> None:
        with pytest.raises(ValueError):
            STAGE_MODELS.read(
                EntryPointValues(
                    entry_point="write", supplied={"stage_models": {"nope": "m"}}
                )
            )


class RecordingManager(SessionManager):
    """A manager that records what a route handed it instead of running a session."""

    def __init__(self) -> None:
        super().__init__()
        self.started = list[EntryPointValues]()

    async def create_session(self, values: EntryPointValues) -> str:
        self.started.append(values)
        return "recorded"

    async def continue_session(self, values: EntryPointValues) -> str:
        self.started.append(values)
        return "recorded"


class TestTheApiHandsOverDeclaredValues:
    """Every route funnels through the declaration rather than restating a field."""

    @pytest.fixture
    def manager(self, monkeypatch: pytest.MonkeyPatch) -> RecordingManager:
        recorded = RecordingManager()
        monkeypatch.setattr(sessions, "get_manager", lambda: recorded)
        return recorded

    async def test_the_endpoint_serves_every_declaration(self) -> None:
        served = await sessions.get_entry_points()
        assert [e.name for e in served] == [e.name for e in ENTRY_POINTS]
        assert served == entry_point_descriptors()

    async def test_creating_a_session_hands_over_what_was_posted(
        self, manager: RecordingManager
    ) -> None:
        body = CreateSessionRequest.model_validate(
            {"sources": ["x"], "light": True, "existing_doc_id": "abc123"}
        )
        launched = await sessions.create_session(body)
        assert launched.session_id == "recorded"
        values = manager.started[0]
        assert LIGHT.read(values) is True
        assert values.declaration.sources(values) == ["x"]

    async def test_resuming_supplies_the_session_the_path_names(
        self, manager: RecordingManager
    ) -> None:
        await sessions.resume_session("20260523_143022", None)
        values = manager.started[0]
        assert values.entry_point == "resume"
        assert values.declaration.resumed_session(values) == "20260523_143022"

    async def test_restarting_supplies_the_session_and_the_stage(
        self, manager: RecordingManager
    ) -> None:
        body = RestartSessionRequest.model_validate({"from_stage": "write"})
        await sessions.restart_session("20260523_143022", body)
        values = manager.started[0]
        assert values.declaration.resumed_session(values) == "20260523_143022"
        assert values.declaration.restart_from(values) == "write"

    async def test_a_value_the_declaration_refuses_is_a_bad_request(
        self, manager: RecordingManager
    ) -> None:
        body = CreateSessionRequest.model_validate(
            {"sources": ["x"], "stop_after": "nonsense"}
        )
        with pytest.raises(HTTPException) as raised:
            await sessions.create_session(body)
        assert raised.value.status_code == 400
        assert manager.started == []


class TestDefaultsComeFromTheDeclaration:
    """No surface restates a default, so an omitted value lands in one place."""

    def test_an_omitted_value_reads_as_its_declared_default(self) -> None:
        from inkwell.environment.entrypoints import STOP_AFTER, TARGET_FORMAT

        values = EntryPointValues.declared("write", {"sources": ["x"]})
        assert TARGET_FORMAT.read(values) == "auto"
        assert STOP_AFTER.read(values) is None
        assert LIGHT.read(values) is False

    def test_a_null_reads_as_its_declared_default(self) -> None:
        values = EntryPointValues.declared(
            "write", {"sources": ["x"], "light": None, "target_format": None}
        )
        from inkwell.environment.entrypoints import TARGET_FORMAT

        assert LIGHT.read(values) is False
        assert TARGET_FORMAT.read(values) == "auto"

    def test_a_value_the_entry_point_does_not_declare_is_dropped(self) -> None:
        values = EntryPointValues.declared(
            "restart", {"resumed_session": "s", "from_stage": "write", "light": True}
        )
        assert "light" not in values.supplied
        assert LIGHT.read(values) is False

    def test_an_entry_point_nobody_declared_is_refused(self) -> None:
        with pytest.raises(KeyError, match="No entry point named"):
            EntryPointValues.declared("teleport", {})
