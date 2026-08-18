"""Every writing entry point, declared once and rendered onto three surfaces.

An entry point is one way a writing session starts: ``write`` from source
material, ``run`` from a freeform task, ``revise`` from an existing draft,
``resume`` and ``restart`` from a saved session. Each declares its parameters
here once — type, default, help text, and which surfaces carry it — and the
three surfaces are compiled from that declaration instead of restated beside
it: the typer commands into ``environment/cli/commands.py``, the API request
models by :func:`request_model` at import time, and the browser form off
``GET /api/entry-points``, which serves :func:`entry_point_descriptors`. A
parameter therefore cannot exist on one surface alone.

The values a surface collects travel as one :class:`EntryPointValues`, and every
parameter reads its own value back off that object, applying its declared
default and its own coercion. That is what keeps the launch path from restating
the parameter set a fourth time, and what makes a rule — a stage name that must
name a checkpoint, a Google Doc URL that must reduce to an id — hold wherever
the value arrived from.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import PurePosixPath
from types import UnionType
from typing import Literal
from urllib.parse import parse_qsl, urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    create_model,
    model_validator,
)
from pydantic.fields import FieldInfo

from lup.types import StringMap

from inkwell.agent.book import ChapterAssignment
from inkwell.agent.config import PIPELINE_STAGES, PipelineStage
from inkwell.agent.models import SourceRole
from inkwell.agent.stages import OUTPUT_FORMATS

type SuppliedValue = str | bool | Sequence[str] | StringMap | None
"""One value a surface collected for a declared parameter, in the shape that
surface had it: a switch, a line of text, several lines, or a mapping.

Deliberately not ``JsonValue``, though every member serializes as JSON: a surface
hands over the concrete container it built — a ``list[str]`` of sources, a
``dict[str, str]`` of overrides — and ``list`` is invariant, so a JSON-shaped
alias would refuse exactly the values every surface has."""

type SuppliedValues = Mapping[str, SuppliedValue]
"""What one surface collected for one entry point, keyed by declared name."""

type CliPresence = Literal["argument", "option", "root", "none"]
"""How the command line carries a parameter: as a positional argument, as an
option on the command, by the root ``--profile`` option every command inherits,
or not at all."""

type FormPresence = Literal["generic", "bespoke", "none"]
"""How the browser carries a parameter: rendered generically off the descriptor
endpoint, hand-written because its widget is bespoke, or not at all."""

type ParameterWidget = Literal["text", "textarea", "lines", "flag", "select", "map"]
"""The control a generically rendered parameter asks the form for."""

type FieldAnnotation = (
    type[str]
    | type[bool]
    | type[None]
    | type[list[str]]
    | type[dict[PipelineStage, str]]
    | UnionType
)
"""The annotations a declared parameter gives a constructed request field."""

type DynamicModelBuilder = Callable[..., type[BaseModel]]
"""Pydantic's model constructor, whose field names are data rather than source.

``create_model`` takes one keyword argument per field, so a caller compiling a
model out of a declaration expands a mapping into it and cannot spell those
keywords statically. Naming the constructor through this alias says exactly
that, and keeps what matters — that the result is a model class — checked.
"""

BUILD_MODEL: DynamicModelBuilder = create_model
"""The constructed-model seam, reached through the alias that explains it."""


def document_id(value: str) -> str:
    """The Google Doc id in ``value``, which may be a URL or already be an id.

    The id is a path segment, so a URL is read as a path rather than scanned as
    a string: ``docs.google.com/document/d/<id>/edit`` yields ``<id>``, and a
    bare id passes through. Declaring the coercion here is what lets every
    surface accept the URL an author actually has in hand.
    """
    if not value.startswith(("http://", "https://")):
        return value
    segments = PurePosixPath(urlparse(value).path).parts
    return next(
        (
            segments[index + 1]
            for index, segment in enumerate(segments)
            if segment == "d" and index + 1 < len(segments)
        ),
        "",
    )


def chapter_assignment(value: str, what: str) -> ChapterAssignment:
    """``value`` read as ``book`` or ``book:chapter``, or a ``ValueError``.

    ``book:chapter`` is the URL scheme grammar — a name, a colon, the rest — so
    it is read with that parser rather than cut apart by hand, and a book id
    carrying hyphens reads the same way ``atlas:4`` does. A bare ``atlas`` is a
    book with the ordinal left open, which the book's own record answers; only
    a colon says the author settled it themselves. What each half may be is the
    assignment's own rule, checked by constructing one: a surface refuses
    ``atlas:0`` and a book id that could name another directory where the author
    typed it, rather than once the run is already under way. ``what`` is the
    parameter asking, so the error names it.
    """
    read = urlparse(value)
    if not read.scheme:
        if read.path.isdecimal():
            raise ValueError(
                f"{what} takes a book, spelled book:chapter where you also mean "
                f"to say which — {value!r} is a chapter with no book to sit in"
            )
        return ChapterAssignment(book=read.path)
    if not read.path.isdecimal():
        raise ValueError(
            f"{what} takes a chapter number after the colon, not {read.path!r} — "
            f"'atlas:4', or bare 'atlas' to let the book's record say which"
        )
    return ChapterAssignment(book=read.scheme, chapter=int(read.path))


def checkpoint_stage(value: str, what: str) -> str:
    """``value`` normalized to a stage a run can pause at, or a ``ValueError``.

    The rule is the pipeline's own, called rather than restated, so the stages a
    surface accepts and the stages a run can actually stop at cannot come apart.
    ``what`` is the parameter asking, so the error names it.
    """
    from inkwell.agent.pipeline import validate_checkpoint_stage

    return validate_checkpoint_stage(value, what=what) or ""


class SurfacePlan(BaseModel):
    """Which surfaces carry one parameter, and why any surface does not.

    A surface marked absent must say why in ``omitted_because``. The reason is
    what makes a parameter one surface leaves out a decision on the record
    rather than the drift nobody notices.
    """

    model_config = ConfigDict(frozen=True)

    cli: CliPresence = "option"
    api: bool = True
    form: FormPresence = "generic"
    omitted_because: str = Field(
        default="",
        description="Why a surface marked absent does not carry this parameter",
    )

    @property
    def absent_somewhere(self) -> bool:
        """Whether any of the three surfaces leaves this parameter out."""
        return self.cli == "none" or not self.api or self.form == "none"

    @model_validator(mode="after")
    def omission_records_its_reason(self) -> "SurfacePlan":
        if self.absent_somewhere and not self.omitted_because:
            raise ValueError(
                "a parameter a surface leaves out must record why in omitted_because"
            )
        if not self.absent_somewhere and self.omitted_because:
            raise ValueError(
                "omitted_because says why a surface leaves a parameter out, and "
                "all three surfaces carry this one"
            )
        return self


class EntryPointParameter(BaseModel):
    """One parameter of one entry point, as every surface spells it.

    The base answers everything a surface asks — the annotation a request field
    takes, the source text a typer signature spells, the widget the browser
    renders — and each kind of value answers for itself, so a new kind of value
    is one subclass rather than an edit to three renderers.

    A kind also declares ``read``, whose return type is its own: a consumer
    holds the declared constant, so it reads a ``bool`` off a flag and a
    ``list[str]`` off a list of strings without narrowing anything itself.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(
        description="Declared name — the request field, the key a value is sent "
        "under, and the name the generated typer signature gives its parameter"
    )
    help: str = Field(description="The sentence every surface shows")
    label: str = Field(
        default="", description="What the form calls it; the name when empty"
    )
    surfaces: SurfacePlan = Field(default_factory=SurfacePlan)
    flags: list[str] = Field(
        default_factory=list,
        description="Option spellings a CLI option is invoked by, longest first",
    )
    required: bool = Field(
        default=False, description="Whether the API and the form must supply a value"
    )
    cli_may_omit: bool = Field(
        default=False,
        description="Whether the command accepts a required parameter's absence "
        "because it asks the author for the value interactively",
    )
    options_endpoint: str = Field(
        default="",
        description="Path under the API root serving this parameter's option "
        "list to the form, for a widget that offers a choice",
    )

    @property
    def help_text(self) -> str:
        """The help every surface shows: the declared sentence, plus whatever
        this kind of value appends to it."""
        return self.help

    @property
    def form_label(self) -> str:
        """What the form calls this parameter."""
        return self.label or self.name

    @property
    def annotation(self) -> FieldAnnotation:
        """The annotation this parameter gives a constructed request field."""
        return str

    @property
    def json_default(self) -> SuppliedValue:
        """This parameter's default, as the wire and a request model carry it."""
        return None

    @property
    def widget(self) -> ParameterWidget:
        """The control a generic form renders this parameter as."""
        return "text"

    @property
    def cli_annotation(self) -> str:
        """The annotation the generated typer signature spells."""
        return "str | None"

    @property
    def cli_default(self) -> str:
        """The default the generated signature spells, empty where it has none."""
        return "None"

    @property
    def cli_required(self) -> bool:
        """Whether the command insists on a value, ordering it before the rest."""
        return self.required and not self.cli_may_omit

    def coerce(self, raw: SuppliedValue) -> SuppliedValue:
        """Normalize and validate one supplied value, or raise ``ValueError``."""
        return raw

    def raw(self, values: "EntryPointValues") -> SuppliedValue:
        """This parameter's supplied value, coerced, or its declared default.

        An absent or null entry means the default, which is why no surface has
        to restate one: a command that leaves an option unset and a request body
        that omits the field arrive at the same value here.
        """
        given = values.supplied[self.name] if self.name in values.supplied else None
        return self.json_default if given is None else self.coerce(given)

    def field_info(self) -> FieldInfo:
        """The metadata this parameter gives a constructed request field.

        A required parameter carries no default, so a body omitting it is
        refused; every other one carries the declared default, so a body omitting
        it lands where a command that left the option unset lands.
        """
        if self.required:
            return FieldInfo(description=self.help_text)
        return FieldInfo(default=self.json_default, description=self.help_text)

    def descriptor(self) -> "ParameterDescriptor":
        """This parameter as the browser receives it."""
        return ParameterDescriptor(
            name=self.name,
            label=self.form_label,
            help=self.help_text,
            widget=self.widget,
            default=self.json_default,
            required=self.required,
            options_endpoint=self.options_endpoint,
            rendered=self.surfaces.form == "generic",
        )


class TextParameter(EntryPointParameter):
    """One line or block of text — a format key, a task, a draft to revise."""

    default: str = ""
    multiline: bool = False

    @property
    def annotation(self) -> FieldAnnotation:
        return str

    @property
    def json_default(self) -> SuppliedValue:
        return self.default

    @property
    def widget(self) -> ParameterWidget:
        return "textarea" if self.multiline else "text"

    @property
    def cli_annotation(self) -> str:
        return "str"

    @property
    def cli_default(self) -> str:
        return "" if self.cli_required else json.dumps(self.default, ensure_ascii=False)

    def coerce(self, raw: SuppliedValue) -> SuppliedValue:
        if not isinstance(raw, str):
            raise ValueError(f"{self.name} takes text")
        return raw

    def read(self, values: "EntryPointValues") -> str:
        """The text supplied for this parameter, or its declared default."""
        raw = self.raw(values)
        return raw if isinstance(raw, str) else self.default


class OptionalTextParameter(EntryPointParameter):
    """Text a surface may leave unset, where unset is not the empty string."""

    @property
    def annotation(self) -> FieldAnnotation:
        return str if self.required else str | None

    @property
    def cli_annotation(self) -> str:
        return "str" if self.cli_required else "str | None"

    @property
    def cli_default(self) -> str:
        return "" if self.cli_required else "None"

    def coerce(self, raw: SuppliedValue) -> SuppliedValue:
        if not isinstance(raw, str):
            raise ValueError(f"{self.name} takes text")
        return raw

    def read(self, values: "EntryPointValues") -> str | None:
        """The text supplied, or None where the surface left it unset."""
        raw = self.raw(values)
        return raw if isinstance(raw, str) and raw else None


class DocumentParameter(OptionalTextParameter):
    """A Google Doc to write into, given as a URL or as a bare document id."""

    def coerce(self, raw: SuppliedValue) -> SuppliedValue:
        if not isinstance(raw, str):
            raise ValueError(f"{self.name} takes a Google Doc URL or id")
        return document_id(raw)


class StageParameter(OptionalTextParameter):
    """A pipeline stage a run pauses after, picks up from, or regenerates.

    Every surface validates against the pipeline's own checkpoint stages, so a
    typo is refused identically wherever it arrived from, and the stage list in
    the help is the pipeline's rather than a copy of it.
    """

    @property
    def help_text(self) -> str:
        from inkwell.agent.pipeline import CHECKPOINT_STAGES

        return f"{self.help} Stages: {', '.join(CHECKPOINT_STAGES)}"

    @property
    def widget(self) -> ParameterWidget:
        return "select"

    def coerce(self, raw: SuppliedValue) -> SuppliedValue:
        if not isinstance(raw, str):
            raise ValueError(f"{self.name} takes a stage name")
        return checkpoint_stage(raw, self.name)


class AssignmentParameter(EntryPointParameter):
    """Which book a run writes, and which chapter of it where the author said.

    One value rather than two, because half of it is not an assignment: a
    chapter with no book is nothing a run can be launched with, and a surface
    able to collect one without the other would have to say what that means
    somewhere else. The other half is genuinely optional — ``atlas`` leaves the
    ordinal to the book's own record — which is why this reads back an
    assignment rather than a placement. Text on the wire, so every surface
    renders it with the control it already has — the option, the request field,
    and the browser's own text box — and it is read back here, once, rather
    than by each of them.

    A kind of its own rather than a text parameter with a rule bolted on: this
    is what its own ``read`` returning an assignment means, and inheriting one
    that returns text would leave every caller narrowing the string again.
    """

    @property
    def annotation(self) -> FieldAnnotation:
        """Text a run either supplies or, where it may, leaves unsaid."""
        return str if self.required else str | None

    @property
    def help_text(self) -> str:
        return (
            f"{self.help} Spelled book:chapter, as in 'atlas:4', or bare "
            f"'atlas' to let the book's own record say which chapter this is."
        )

    @property
    def cli_annotation(self) -> str:
        return "str" if self.cli_required else "str | None"

    @property
    def cli_default(self) -> str:
        return "" if self.cli_required else "None"

    def coerce(self, raw: SuppliedValue) -> SuppliedValue:
        if not isinstance(raw, str):
            raise ValueError(f"{self.name} takes a book, or a book and a chapter")
        if not raw:
            return ""
        return chapter_assignment(raw, self.name).spelled()

    def read(self, values: "EntryPointValues") -> ChapterAssignment | None:
        """Which book this run writes a chapter of, or None where it writes none.

        None is a standalone piece, not a book of one chapter: a run says which
        book it belongs to or belongs to none.
        """
        raw = self.raw(values)
        if not isinstance(raw, str) or not raw:
            return None
        return chapter_assignment(raw, self.name)


class FlagParameter(EntryPointParameter):
    """A boolean the command spells as a switch and the form as a checkbox."""

    default: bool = False

    @property
    def annotation(self) -> FieldAnnotation:
        return bool

    @property
    def json_default(self) -> SuppliedValue:
        return self.default

    @property
    def widget(self) -> ParameterWidget:
        return "flag"

    @property
    def cli_annotation(self) -> str:
        return "bool"

    @property
    def cli_default(self) -> str:
        return repr(self.default)

    def coerce(self, raw: SuppliedValue) -> SuppliedValue:
        if not isinstance(raw, bool):
            raise ValueError(f"{self.name} is a switch: it takes true or false")
        return raw

    def read(self, values: "EntryPointValues") -> bool:
        """Whether this switch was set, or its declared default."""
        raw = self.raw(values)
        return raw if isinstance(raw, bool) else self.default


class TextListParameter(EntryPointParameter):
    """Several strings — sources, references. One per line in the form."""

    @property
    def annotation(self) -> FieldAnnotation:
        return list[str]

    @property
    def json_default(self) -> SuppliedValue:
        return []

    @property
    def widget(self) -> ParameterWidget:
        return "lines"

    @property
    def cli_annotation(self) -> str:
        return "list[str] | None"

    def coerce(self, raw: SuppliedValue) -> SuppliedValue:
        if not isinstance(raw, list):
            raise ValueError(f"{self.name} takes a list of strings")
        return [item for item in raw if isinstance(item, str) and item.strip()]

    def read(self, values: "EntryPointValues") -> list[str]:
        """The strings supplied for this parameter, empty where none were."""
        raw = self.raw(values)
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, str)]


class StageModelPair(BaseModel):
    """One per-stage model override, as a command line spells it."""

    model_config = ConfigDict(frozen=True)

    stage: str = Field(description="The pipeline stage the override applies to")
    runs_on: str = Field(description="The model id that stage runs on")

    @classmethod
    def parsed(cls, text: str, parameter: str) -> "StageModelPair":
        """One ``stage=model`` entry, or a ``ValueError`` naming the stages.

        ``stage=model`` is the query-string grammar, so it is read with that
        parser: one pair, both halves present, or the option is refused.
        """
        pairs = parse_qsl(text)
        if len(pairs) != 1:
            raise ValueError(
                f"{parameter} takes stage=model pairs, not {text!r}. "
                f"Stages: {', '.join(PIPELINE_STAGES)}"
            )
        stage, runs_on = pairs[0]
        return cls(stage=stage, runs_on=runs_on)


STAGE_MODELS_ADAPTER = TypeAdapter(dict[PipelineStage, str])
"""Validates a per-stage override map, whichever surface assembled it."""


class StageModelParameter(EntryPointParameter):
    """Per-stage model overrides: a pipeline stage to the model id it runs on.

    The command spells it as a repeatable ``stage=model`` option and the API as
    a mapping, and both land on the same declared value — which is the point of
    letting a kind of value answer for each surface separately.
    """

    @property
    def annotation(self) -> FieldAnnotation:
        return dict[PipelineStage, str]

    @property
    def json_default(self) -> SuppliedValue:
        return {}

    @property
    def widget(self) -> ParameterWidget:
        return "map"

    @property
    def cli_annotation(self) -> str:
        return "list[str] | None"

    def coerce(self, raw: SuppliedValue) -> SuppliedValue:
        if isinstance(raw, dict):
            return self.checked({key: str(value) for key, value in raw.items()})
        if not isinstance(raw, list):
            raise ValueError(f"{self.name} takes stage=model pairs")
        pairs = [StageModelPair.parsed(str(item), self.name) for item in raw]
        return self.checked({pair.stage: pair.runs_on for pair in pairs})

    def checked(self, overrides: StringMap) -> StringMap:
        """``overrides`` with every stage name checked against the pipeline's.

        The check belongs in ``coerce`` rather than in ``read`` because ``coerce``
        is what every surface passes through: a stage nobody declared has to be
        refused while a command can still report it as a usage error, rather than
        once the run is already under way. The adapter decides; the message names
        which stage it objected to, the way every other stage error reads.
        """
        try:
            STAGE_MODELS_ADAPTER.validate_python(overrides)
        except ValidationError as exc:
            named = ", ".join(
                stage for stage in overrides if stage not in PIPELINE_STAGES
            )
            raise ValueError(
                f"Invalid {self.name} stage '{named}'. "
                f"Stages: {', '.join(PIPELINE_STAGES)}"
            ) from exc
        return overrides

    def read(self, values: "EntryPointValues") -> dict[PipelineStage, str]:
        """The overrides supplied, validated against the pipeline's stage names."""
        return STAGE_MODELS_ADAPTER.validate_python(self.raw(values))


class ParameterDescriptor(BaseModel):
    """One parameter as the browser receives it, rendered off the declaration."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(description="Declared name, and the key a value is sent under")
    label: str = Field(description="What the form calls this parameter")
    help: str = Field(description="The sentence shown beside the control")
    widget: ParameterWidget = Field(description="The control to render")
    default: SuppliedValue = Field(description="The value the control starts at")
    required: bool = Field(description="Whether the form must supply a value")
    options_endpoint: str = Field(
        description="Path under the API root serving the option list, or empty"
    )
    rendered: bool = Field(
        description="Whether the generic renderer owns this control, as opposed "
        "to the page carrying bespoke UI for it"
    )


class EntryPointDescriptor(BaseModel):
    """One entry point as the browser receives it."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(description="Entry point name, as the request path spells it")
    summary: str = Field(description="One line describing what it starts")
    detail: str = Field(default="", description="The longer explanation, if any")
    starts_a_session: bool = Field(
        description="Whether this begins a session rather than continuing a saved "
        "one — which page offers it follows from this, not from which parameters "
        "it happens to take"
    )
    parameters: list[ParameterDescriptor] = Field(
        description="Every parameter the form carries, generic ones and bespoke"
    )


class EntryPoint(BaseModel):
    """One way a writing session starts, declared once for every surface.

    The base answers what the launch path asks of any entry point and declines
    what does not apply — a fresh run continues no session, a resume supplies no
    sources — so a new entry point is one subclass and one declaration rather
    than an arm added to every walk over them.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(description="Command name, request path, and descriptor key")
    summary: str = Field(description="The command's first docstring line")
    detail: str = Field(default="", description="The rest of the command's docstring")
    parameters: list[EntryPointParameter] = Field(
        description="Every parameter this entry point takes, on any surface"
    )
    skipped_stages: list[str] = Field(
        default=[],
        description=(
            "Backbone stages this entry point's runs do not perform. Declared "
            "here so the pipeline reads one list, rather than each stage "
            "growing a conditional about which entry point launched it — a "
            "second entry point skipping the same stage would otherwise have "
            "to find every one of them. Empty for one that skips nothing, and "
            "for one continuing a saved run, which takes the stages it left"
        ),
    )

    material_role: SourceRole = Field(
        default="source",
        description=(
            "What this entry point's material is to the run. Declared because "
            "the command already knows — a draft handed to `revise` is the "
            "piece being replaced — where a stage downstream could only guess "
            "from prose that reads the same either way. 'source' for the "
            "entry points whose material really is content to write from."
        ),
    )

    @model_validator(mode="after")
    def skips_name_real_stages(self) -> "EntryPoint":
        """Refuse a skip naming no stage, which would silently skip nothing."""
        unknown = [s for s in self.skipped_stages if s not in PIPELINE_STAGES]
        if unknown:
            raise ValueError(
                f"{self.name} skips {', '.join(unknown)}, which name no stage; "
                f"the stages are {', '.join(PIPELINE_STAGES)}"
            )
        return self

    @property
    def command_parameters(self) -> list[EntryPointParameter]:
        """The parameters the generated command spells, in signature order.

        A Python signature admits no defaulted parameter before an undefaulted
        one, so what a command insists on comes first whatever kind it is, and
        positional arguments lead within each group. Sorting on the default
        first is what makes the compiled signature valid by construction rather
        than valid because no declaration happens to mix the two today.
        """
        return sorted(
            (p for p in self.parameters if p.surfaces.cli in ("argument", "option")),
            key=lambda p: (bool(p.cli_default), p.surfaces.cli != "argument"),
        )

    @property
    def api_parameters(self) -> list[EntryPointParameter]:
        """The parameters the constructed request model carries."""
        return [p for p in self.parameters if p.surfaces.api]

    @property
    def form_parameters(self) -> list[EntryPointParameter]:
        """The parameters the browser carries, generically rendered or bespoke."""
        return [p for p in self.parameters if p.surfaces.form != "none"]

    @property
    def starts_a_session(self) -> bool:
        """Whether this begins a session rather than continuing a saved one."""
        return False

    def descriptor(self) -> EntryPointDescriptor:
        """This entry point as ``GET /api/entry-points`` serves it."""
        return EntryPointDescriptor(
            name=self.name,
            summary=self.summary,
            detail=self.detail,
            starts_a_session=self.starts_a_session,
            parameters=[p.descriptor() for p in self.form_parameters],
        )

    def sources(self, values: "EntryPointValues") -> list[str]:
        """The source material this entry point hands the pipeline."""
        return []

    def assignment(self, values: "EntryPointValues") -> ChapterAssignment | None:
        """Which book this entry point's runs write a chapter of, where any does.

        Answered by the entry point rather than read off one declared parameter,
        because a run whose whole subject is one chapter spells it as its leading
        argument while every other entry point spells it as an option — and the
        launch path should not have to know which of those it is looking at.
        """
        return None

    def resumed_session(self, values: "EntryPointValues") -> str | None:
        """The saved session this entry point continues, or None for a fresh run."""
        return None

    def resume_from(self, values: "EntryPointValues") -> str | None:
        """The stage a continued run picks up after, where it names one."""
        return None

    def restart_from(self, values: "EntryPointValues") -> str | None:
        """The stage a continued run regenerates from scratch, where it names one."""
        return None

    def with_material(
        self, values: "EntryPointValues", text: str
    ) -> "EntryPointValues":
        """These values with ``text`` as the material a command asked for.

        Only an entry point declaring ``cli_may_omit`` on its material reaches
        this: the rest insist on it, so there is nothing for them to answer.
        """
        return values


class EntryPointValues(BaseModel):
    """The raw values one invocation of an entry point supplied.

    A surface collects values under the declared names and hands them here
    untouched; every parameter reads its own value back off this object,
    applying its declared default and its own coercion. That is why no surface
    and no launch path restates a default or a validation rule.
    """

    model_config = ConfigDict(frozen=True)

    entry_point: str = Field(description="Which declared entry point was invoked")
    supplied: dict[str, SuppliedValue] = Field(
        default_factory=dict,
        description="Values the surface collected, keyed by declared name; an "
        "absent or null entry means the declared default",
    )

    @classmethod
    def declared(cls, entry_point: str, supplied: SuppliedValues) -> "EntryPointValues":
        """One surface's values, narrowed and checked against what it names.

        Anything the entry point does not declare is dropped, and every value it
        does is coerced once here — so a ``KeyError`` for an entry point nobody
        declared and a ``ValueError`` for a value a parameter refuses both surface
        at the boundary rather than deep inside the pipeline.
        """
        declaration = entry_point_named(entry_point)
        values = cls(
            entry_point=entry_point,
            supplied={
                parameter.name: supplied[parameter.name]
                for parameter in declaration.parameters
                if parameter.name in supplied
            },
        )
        for parameter in declaration.parameters:
            parameter.raw(values)
        return values

    @property
    def declaration(self) -> EntryPoint:
        """The entry point these values were collected for."""
        return entry_point_named(self.entry_point)

    def replacing(
        self, parameter: EntryPointParameter, value: SuppliedValue
    ) -> "EntryPointValues":
        """These values with one parameter's value substituted."""
        return EntryPointValues(
            entry_point=self.entry_point,
            supplied={**self.supplied, parameter.name: value},
        )


class FreshEntryPoint(EntryPoint):
    """An entry point that starts a new session from material it is given."""

    standing_instruction: str = Field(
        default="",
        description="An instruction this entry point always adds beside the "
        "material, saying what the run is for",
    )

    @property
    def starts_a_session(self) -> bool:
        return True

    def assignment(self, values: EntryPointValues) -> ChapterAssignment | None:
        return CHAPTER.read(values)

    def material(self, values: EntryPointValues) -> list[str]:
        """The material the author supplied, before the standing instruction."""
        return []

    def sources(self, values: EntryPointValues) -> list[str]:
        supplied = self.material(values)
        if not supplied or not self.standing_instruction:
            return supplied
        return [*supplied, self.standing_instruction]


class ContinuedEntryPoint(EntryPoint):
    """An entry point that picks a saved session up rather than starting one."""

    def resumed_session(self, values: EntryPointValues) -> str | None:
        return RESUMED_SESSION.read(values)


# ---------------------------------------------------------------------------
# The declared parameters, shared by every entry point that takes them
# ---------------------------------------------------------------------------

SOURCES = TextListParameter(
    name="sources",
    label="Source material",
    help="Source materials: Claude share links, URLs, file paths, or freeform text",
    required=True,
    cli_may_omit=True,
    surfaces=SurfacePlan(cli="argument", form="bespoke"),
)

TASK = TextParameter(
    name="task",
    label="Task",
    help="Freeform task for the agent — what to write, in your own words",
    required=True,
    multiline=True,
    surfaces=SurfacePlan(cli="argument"),
)

DRAFT = TextParameter(
    name="draft",
    label="Draft to revise",
    help="The section to revise: a Google Doc URL, a file path, a URL, or the "
    "draft text itself",
    required=True,
    multiline=True,
    surfaces=SurfacePlan(cli="argument"),
)

REFS = TextListParameter(
    name="refs",
    label="References",
    help="Supplementary reference URLs or file paths",
    flags=["--ref", "-r"],
    surfaces=SurfacePlan(form="bespoke"),
)

TARGET_FORMAT = TextParameter(
    name="target_format",
    label="Output format",
    help="Suggested format (the agent may override): "
    + ", ".join(
        f"{f.key}:<description>" if f.accepts_description else f.key
        for f in OUTPUT_FORMATS
    ),
    default="auto",
    flags=["--format", "-f"],
    surfaces=SurfacePlan(form="bespoke"),
)

CHAPTER = AssignmentParameter(
    name="chapter",
    label="Chapter of a book",
    help="Which chapter of which book this run writes, so it reads what the "
    "book's other chapters recorded and files its own record beside them. "
    "Leave it unset for a standalone piece, which belongs to no book.",
    flags=["--chapter"],
)

CHAPTER_WRITTEN = AssignmentParameter(
    name="chapter",
    label="Chapter of a book",
    help="Which chapter of which book to write, on its own — the book's "
    "recorded order and cross-references are read rather than laid out again.",
    required=True,
    surfaces=SurfacePlan(cli="argument"),
)
"""The same value the option carries, spelled as the subject of its command.

Sent under the same declared name, so one launch path reads either — what
differs is that a command whose whole purpose is one chapter insists on it and
puts it first, rather than accepting a run that named no book at all.
"""

EXISTING_DOC_ID = DocumentParameter(
    name="existing_doc_id",
    label="Existing Google Doc",
    help="Google Doc URL or id to write into, instead of creating a new document",
    flags=["--doc", "-d"],
)

STOP_AFTER = StageParameter(
    name="stop_after",
    label="Pause after stage",
    help="Pause once this stage finishes and exit cleanly — review and comment "
    "in the Doc, then resume the session to continue.",
    flags=["--stop-after"],
    options_endpoint="stop-stages",
)

LIGHT = FlagParameter(
    name="light",
    label="Light pipeline",
    help="Run the light pipeline: a single writer, fact-check-only review, and no "
    "deep research, resolve, or rewrite. Implied by the LinkedIn format.",
    flags=["--light"],
)

PROFILE = OptionalTextParameter(
    name="profile",
    label="Profile",
    help="Configuration profile to run under, whose account inference is billed to",
    surfaces=SurfacePlan(cli="root", form="bespoke"),
)

MODEL = OptionalTextParameter(
    name="model",
    label="Default model",
    help="Model for every stage, overriding the profile's default",
    flags=["--model"],
    surfaces=SurfacePlan(form="bespoke"),
)

WRITER_MODE = OptionalTextParameter(
    name="writer_mode",
    label="Writer mode",
    help="Draft production mode: 'parallel' (one writer per section, then merge) "
    "or 'single' (one writer drafts the whole piece)",
    flags=["--writer-mode"],
    surfaces=SurfacePlan(form="bespoke"),
)

STAGE_MODELS = StageModelParameter(
    name="stage_models",
    label="Per-stage models",
    help="Per-stage model override, as stage=model; repeat the option per stage",
    flags=["--stage-model"],
    surfaces=SurfacePlan(form="bespoke"),
)

SESSION_ID = OptionalTextParameter(
    name="session_id",
    help="Session identifier to run under, instead of a freshly minted one",
    flags=["--session-id", "-s"],
    surfaces=SurfacePlan(
        api=False,
        form="none",
        omitted_because="the server mints the id, so two clients cannot collide "
        "on one, and returns it in the response",
    ),
)

VERBOSE = FlagParameter(
    name="verbose",
    help="Enable verbose logging",
    flags=["--verbose", "-v"],
    surfaces=SurfacePlan(
        api=False,
        form="none",
        omitted_because="terminal-only: the server's log level is its own "
        "configuration rather than something one request turns up",
    ),
)

RESUMED_SESSION = OptionalTextParameter(
    name="resumed_session",
    help="Session id to continue (see `inkwell sessions`)",
    required=True,
    surfaces=SurfacePlan(
        cli="argument",
        api=False,
        form="none",
        omitted_because="the session is the resource the request path names, and "
        "the page the form sits on already knows which one it is",
    ),
)

RESUME_FROM = StageParameter(
    name="from_stage",
    label="Resume from after stage",
    help="Pick up after this stage instead of where the run stopped.",
    flags=["--from", "-f"],
    surfaces=SurfacePlan(form="bespoke"),
)

RESTART_FROM = StageParameter(
    name="from_stage",
    label="Regenerate from stage",
    help="Re-run this stage and everything after it from scratch with fresh "
    "agents, discarding their prior output.",
    required=True,
    flags=["--from", "-f"],
    surfaces=SurfacePlan(form="bespoke"),
)


# ---------------------------------------------------------------------------
# The declared entry points
# ---------------------------------------------------------------------------

CONFIGURATION_PARAMETERS = [PROFILE, MODEL, WRITER_MODE, STAGE_MODELS]
"""The profile and model overrides every entry point accepts."""


class WriteEntryPoint(FreshEntryPoint):
    """Writes from the source material the author supplies."""

    def material(self, values: EntryPointValues) -> list[str]:
        return SOURCES.read(values)

    def with_material(self, values: EntryPointValues, text: str) -> EntryPointValues:
        return values.replacing(SOURCES, [text])


class RunEntryPoint(FreshEntryPoint):
    """Writes from a freeform task, used as the planner's source material."""

    def material(self, values: EntryPointValues) -> list[str]:
        return [TASK.read(values)]


class ReviseEntryPoint(FreshEntryPoint):
    """Rewrites an existing draft through the ordinary writing pipeline."""

    def material(self, values: EntryPointValues) -> list[str]:
        return [DRAFT.read(values)]


class ChapterEntryPoint(FreshEntryPoint):
    """Writes one chapter of a book, against the order the book already holds.

    The book it belongs to is its leading argument rather than an option: a run
    that named no book would be this command with nothing left of it.
    """

    def assignment(self, values: EntryPointValues) -> ChapterAssignment | None:
        return CHAPTER_WRITTEN.read(values)

    def material(self, values: EntryPointValues) -> list[str]:
        return SOURCES.read(values)

    def with_material(self, values: EntryPointValues, text: str) -> EntryPointValues:
        return values.replacing(SOURCES, [text])


class ResumeEntryPoint(ContinuedEntryPoint):
    """Continues a saved session, picking an interrupted agent up mid-task."""

    def resume_from(self, values: EntryPointValues) -> str | None:
        return RESUME_FROM.read(values)


class RestartEntryPoint(ContinuedEntryPoint):
    """Rewinds a saved session and regenerates a stage onward with fresh agents."""

    def restart_from(self, values: EntryPointValues) -> str | None:
        return RESTART_FROM.read(values)


REVISE_INSTRUCTION = """\
Revise the draft above. It is the piece this run REPLACES: what you produce \
stands in its place, so rewrite it for clarity and currency — sharpen what is \
vague, cut what does not earn its place, and bring every fact, figure, and \
reference up to date. Keep the author's voice and the argument they are making; \
everything else about the draft is yours to change.

The draft being finished prose is not a reason to tread lightly. Published \
text, a textbook chapter, something already carrying its own citations — that \
is the ordinary case for a revision, and it is what you were pointed at. \
Leaving a passage alone because a reader could already look it up elsewhere is \
the one outcome this run has no use for.

Plan it as the piece it already is: its sections are the draft's sections, and \
its research questions start from the draft's own claims asked again — for \
every fact, figure, and citation the draft rests on, ask whether it still holds \
and whether anything has superseded it. A revision that only re-proses a stale \
claim has failed at the thing it was for.

Then ask what the draft does not raise at all. A draft is fixed at the moment \
it was written, so the gap that matters most is usually not a stale claim but a \
missing one: the case, result, or event that arrived afterwards and that a \
reader of this piece today would expect it to cover. Ask for those directly, by \
subject rather than by claim, because no question derived from the draft can \
reach them and nothing later in this pipeline can add what research never went \
looking for."""
"""What the revise entry point always asks for, beside the draft it is given.

Carrying the currency check here rather than in a stage is what keeps revise a
declaration: the planner reads it through the ordinary source path, so no stage
had to learn which entry point launched the run. The research questions follow
from the draft's own citations because this says to derive them that way —
which is also why revise skips no stage, since research is where that check is
actually spent.

What the instruction cannot do alone is stop the preservation reviewers
scoring the rewrite as damage — a cut reads as a dropped specific and a
departure reads as infidelity to the source. That is why the draft also
travels under its own role: see ``material_role`` and ``revision_target``.
"""


WRITE = WriteEntryPoint(
    name="write",
    summary="Write an article from source material.",
    detail="""\
Each source can be a Claude.ai share link, a URL to extract content from, a
local file path, or a writing brief. Several sources are extracted and combined
for the pipeline.

Examples:
    inkwell write "https://claude.ai/share/abc123"
    inkwell write "https://claude.ai/share/abc123" paper.pdf -f twitter
    inkwell write conversation.md --stop-after plan   # pause to review the plan""",
    parameters=[
        SOURCES,
        REFS,
        TARGET_FORMAT,
        CHAPTER,
        EXISTING_DOC_ID,
        STOP_AFTER,
        LIGHT,
        SESSION_ID,
        VERBOSE,
        *CONFIGURATION_PARAMETERS,
    ],
)

RUN = RunEntryPoint(
    name="run",
    summary="Write from a freeform task rather than from source links.",
    detail="""\
The task text is the planner's source material: it extracts a structure and runs
the ordinary pipeline. For source links and files, use `inkwell write`.

Examples:
    inkwell run "write a blog post about tokenizer economics\"""",
    parameters=[
        TASK,
        TARGET_FORMAT,
        CHAPTER,
        EXISTING_DOC_ID,
        STOP_AFTER,
        LIGHT,
        SESSION_ID,
        VERBOSE,
        *CONFIGURATION_PARAMETERS,
    ],
)

REVISE = ReviseEntryPoint(
    name="revise",
    summary="Revise an existing draft for clarity and currency.",
    detail="""\
Takes a section you already have — a Doc, a file, a URL, or the text itself —
and runs it through the ordinary writing pipeline as the source, under a
standing instruction to rewrite it for clarity and currency without changing its
voice or its argument.

Examples:
    inkwell revise draft.md
    inkwell revise chapter4.md --chapter atlas:4   # one chapter of a book
    inkwell revise "https://docs.google.com/document/d/abc123/edit\"""",
    standing_instruction=REVISE_INSTRUCTION,
    material_role="revision_target",
    parameters=[
        DRAFT,
        REFS,
        TARGET_FORMAT,
        CHAPTER,
        EXISTING_DOC_ID,
        STOP_AFTER,
        LIGHT,
        SESSION_ID,
        VERBOSE,
        *CONFIGURATION_PARAMETERS,
    ],
)

CHAPTER_ALONE = ChapterEntryPoint(
    name="chapter",
    summary="Write one chapter of a book, without laying the book out again.",
    detail="""\
The book's recorded order and its cross-references are read rather than
re-derived, so a chapter written on its own cannot renumber the book around it
or invent an order the other chapters never agreed to. Name the ordinal to place
the chapter yourself; name the book alone and its own record says which chapter
carries this title. A book with no record yet is not an error — the chapter is
appended and the run says so.

Examples:
    inkwell chapter atlas:4 chapter4.md
    inkwell chapter atlas notes.md   # the book's record says which chapter""",
    parameters=[
        CHAPTER_WRITTEN,
        SOURCES,
        REFS,
        TARGET_FORMAT,
        EXISTING_DOC_ID,
        STOP_AFTER,
        LIGHT,
        SESSION_ID,
        VERBOSE,
        *CONFIGURATION_PARAMETERS,
    ],
    skipped_stages=["book"],
)

RESUME = ResumeEntryPoint(
    name="resume",
    summary="Resume a previous writing session from its saved pipeline state.",
    detail="""\
By default picks up from the last completed stage. Use --from to resume from an
earlier checkpoint, and --stop-after to pause again at a later one.

Examples:
    inkwell sessions                              # find the session id
    inkwell resume 20260523_143022                # resume from the last stage
    inkwell resume 20260523_143022 --from write   # re-run merge onward""",
    parameters=[
        RESUMED_SESSION,
        RESUME_FROM,
        STOP_AFTER,
        VERBOSE,
        *CONFIGURATION_PARAMETERS,
    ],
)

RESTART = RestartEntryPoint(
    name="restart",
    summary="Re-run a stage of a saved session from scratch with fresh agents.",
    detail="""\
Unlike resume, which continues an interrupted agent's own conversation, restart
rewinds to the checkpoint before the named stage and regenerates that stage and
everything after it, discarding their prior output.

Examples:
    inkwell restart 20260523_143022 --from write""",
    parameters=[
        RESUMED_SESSION,
        RESTART_FROM,
        VERBOSE,
        *CONFIGURATION_PARAMETERS,
    ],
)

ENTRY_POINTS: list[EntryPoint] = [WRITE, RUN, REVISE, CHAPTER_ALONE, RESUME, RESTART]
"""Every way a writing session starts. The one list all three surfaces read."""


def entry_point_named(name: str) -> EntryPoint:
    """The declared entry point called ``name``, or a ``KeyError`` naming them."""
    found = next((entry for entry in ENTRY_POINTS if entry.name == name), None)
    if found is None:
        raise KeyError(
            f"No entry point named {name!r}. Declared: "
            f"{', '.join(entry.name for entry in ENTRY_POINTS)}"
        )
    return found


def entry_point_descriptors() -> list[EntryPointDescriptor]:
    """Every entry point as ``GET /api/entry-points`` serves it."""
    return [entry.descriptor() for entry in ENTRY_POINTS]


def request_model(entry_point: EntryPoint) -> type[BaseModel]:
    """The model one entry point's API surface validates a request body against.

    Constructed from the declaration at import time, so the fields a request
    carries are the parameters the entry point declares and there is no second
    statement of them to fall behind. A route names the model by subclassing it,
    which is what gives a request body an annotation without respelling a field.
    """
    return BUILD_MODEL(
        f"{entry_point.name.capitalize()}SessionRequest",
        __doc__=f"{entry_point.summary} Rendered from the entry point declaration.",
        **{
            parameter.name: (parameter.annotation, parameter.field_info())
            for parameter in entry_point.api_parameters
        },
    )
