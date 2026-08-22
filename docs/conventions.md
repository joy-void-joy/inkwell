<!-- Generated from lup.devtools.harness.content.docs.conventions by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. -->

# Code Conventions

The guidance states each rule; this page is the lookup it points at. Nothing
here introduces a convention of its own — where a row and the guidance seem to
disagree, the guidance is the statement of intent and this is only its index.

## Primary libraries

| Library | What it is for |
| --- | --- |
| `lup` | The runtime an application composes against, and it is provider-neutral: `SessionFactory` opens a `Session`, a `TurnRequest` carries the prompt and the type the answer must arrive as, and a strict `TurnResult[T]` hands back `.output` already validated. `SessionFactory.query(prompt, Model)` is the whole of a one-shot. |
| [pydantic](https://docs.pydantic.dev/) | Validation, and every model we declare. |
| [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) | Configuration, in place of dotenv. |

A provider's SDK is one adapter's dependency behind an extra — `lup[claude]`,
`lup[codex]` — and never the application's. Importing one here pins the
application to a single runtime and trips `seam-boundary` outside a
composition root that names it, which is why no module under the application
package imports one.

## Typed stand-ins for dict-shaped data

`Any`, `dict[str, Any]`, and `dict[str, object]` are never the answer. Which
type replaces one depends on where the dict came from:

| Where the data comes from | What to use instead |
| --- | --- |
| JSON whose schema lives elsewhere — tool arguments, JSON Schemas, structured outputs, vendor payloads | `JsonValue` / `JsonObject` from `lup.types` |
| An MCP tool's input | The `BaseModel` on the handler's first parameter — `@lup_tool` validates against it before the handler runs |
| An MCP tool's output | The `BaseModel` the handler returns; the decorator serializes it |
| A hook's input | `LupHookInput` from `lup.hooks` |
| A hook's return value | `LupHookOutput`, built by `allow_hook` / `ask_hook` / `deny_hook` / `block_hook` |
| A structured turn result | `TurnResult[Model].output`, already validated against the turn's `output_type` |

The neutral types to prefer over a hand-rolled shape are lup's own:
`LupHookMatcher` and `LupHooksConfig` for registration, `LupMcpTool` and
`LupMcpServerConfig` for tools and the servers they group into,
`ClaudeSessionConfig` / `CodexSessionConfig` for what one runtime's session
takes. Each adapter translates its backend's native identities onto these, so
one vocabulary reads across hooks, policy, and harness declarations.

## Tool input schemas

One input model is the single declaration that both the `@lup_tool` schema and
the runtime validation are taken from. The decorator infers each schema from
the handler's annotations, validates the input before the handler runs, and
serializes the returned model — so a handler that validates its own arguments
or assembles its own response envelope is doing work already done for it:

| Do this | Not this |
| --- | --- |
| `class SearchInput(BaseModel): query: str = Field(description="...")` | `{"query": str, "limit": int}` |
| Annotate the handler's parameter and let `@lup_tool` infer the schema | A hand-written dict schema, or `model_json_schema()` spliced in by hand |
| Take the validated model as the parameter, then `params.query` | `SearchInput.model_validate(args)`, or `args.get("query", "")` |
| Return a `BaseModel` | Assemble the `is_error` envelope yourself |
| `raise ToolError("what to do about it")` | Return a string describing the failure |

`Field(description=...)` is the agent's only documentation for a field, so it
carries what the agent needs to fill the field rather than what the type
already says.

## A parser per format

Reaching for `re`, `.replace()`, `.split()`, or string slicing to take apart
structured data means the structured API was missed:

| Format | Reach for |
| --- | --- |
| Web pages | `trafilatura` for the text, `beautifulsoup4` for the DOM |
| XML | `xml.etree.ElementTree`, or `lxml` |
| JSON | `json.loads()` |
| A completed turn | `TurnResult.output` for the typed answer; filter `TurnResult.blocks` by type and attribute for the prose |
| Dates | Parse to `datetime`; never compare the strings |
| URLs | `urllib.parse` |
| Filesystem paths | `pathlib.Path`, never concatenation |

## Naming without private prefixes

Nothing is private, so nothing carries a leading underscore — at any scope:

| Write | Not |
| --- | --- |
| `build_options` | `_build_options` |
| `remove_stale_container` | `_remove_stale_container` |
| `PACE_THRESHOLDS` | `_PACE_THRESHOLDS` |
| `PendingReminder` | `_PendingReminder` |

A helper that genuinely should not reach the module namespace is nested inside
its only caller rather than marked private:

```python
def build_display(usage, stats):
    def place_label(text, position, width):
        ...
    # use place_label here
```

An unused parameter — `_context`, `_exc_type` — keeps its underscore. That is
a linting convention, not a privacy one.

## What each code-intelligence tool answers

A name has a definition, a scope, and a set of references, and only a resolver
knows them:

| Tool | Contract |
| --- | --- |
| `find_definition` | Find where a symbol is defined. Use instead of grepping for `def name` or `class name`: this resolves imports and aliases, so it finds the real declaration rather than a line that looks like one. |
| `find_references` | Find every use of a symbol across the workspace. Use instead of grepping for a name: this excludes look-alikes in other scopes and includes uses reached through an alias or a re-export. |
| `hover` | Read a symbol&#x27;s inferred type and documentation. Use before assuming what a value is: the checker knows the type that was resolved. |
| `list_symbols` | List every symbol a file declares, with its line. Use instead of grepping for `def ` or `class ` to learn a file&#x27;s shape. |
| `rename_symbol` | Plan a workspace-wide rename of the symbol at a position. Reports the files and edit counts without writing anything. Always prefer this over a find-and-replace, which cannot tell one scope from another. |
