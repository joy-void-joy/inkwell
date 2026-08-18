<!-- Generated from lup.devtools.harness.content.docs.conventions by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. -->

# Code Conventions

The guidance states each rule; this page is the lookup it points at. Nothing
here introduces a convention of its own — where a row and the guidance seem to
disagree, the guidance is the statement of intent and this is only its index.

## Primary libraries

| Library | What it is for |
| --- | --- |
| [claude-agent-sdk](https://github.com/anthropics/claude-agent-sdk-python) | The agent framework. `query()` is the one-shot call, with structured output. |
| [pydantic](https://docs.pydantic.dev/) | Validation, and every model we declare. |
| [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) | Configuration, in place of dotenv. |

## Typed stand-ins for dict-shaped data

`Any`, `dict[str, Any]`, and `dict[str, object]` are never the answer. Which
type replaces one depends on where the dict came from:

| Where the data comes from | What to use instead |
| --- | --- |
| JSON whose schema lives elsewhere — tool arguments, JSON Schemas, structured outputs, vendor payloads | `JsonValue` / `JsonObject` from `lup.types` |
| An MCP tool's input | `BaseModel.model_validate(args)` at the top of the handler; the raw dict never travels further |
| An MCP tool's output | A `TypedDict` naming the returned shape |
| A hook's return value | `SyncHookJSONOutput` from `claude_agent_sdk.types` |
| A hook's input | The SDK's typed input model for that event, with its matching hook-specific output model |

SDK types to prefer over a hand-rolled shape: `HookMatcher`,
`AgentDefinition`, `ClaudeAgentOptions`, `McpServerConfig`,
`PermissionResultAllow` and `PermissionResultDeny`, `ContentBlock`, `Message`,
`TextBlock`, `ToolUseBlock`, `ToolResultBlock`. Import from the top-level
`claude_agent_sdk` wherever the name is exported there; `SyncHookJSONOutput`,
`HookEvent`, and the per-event hook types come from `claude_agent_sdk.types`.

## Tool input schemas

One input model is the single declaration that both the `@tool` schema and the
runtime validation are taken from:

| Do this | Not this |
| --- | --- |
| `class SearchInput(BaseModel): query: str = Field(description="...")` | `{"query": str, "limit": int}` |
| `SearchInput.model_json_schema()` for the `@tool` schema | A hand-written dict schema |
| `SearchInput.model_validate(args)`, then `params.query` | `args.get("query", "")` |

## A parser per format

Reaching for `re`, `.replace()`, `.split()`, or string slicing to take apart
structured data means the structured API was missed:

| Format | Reach for |
| --- | --- |
| Web pages | `trafilatura` for the text, `beautifulsoup4` for the DOM |
| XML | `xml.etree.ElementTree`, or `lxml` |
| JSON | `json.loads()` |
| SDK objects | Filter the `ContentBlock` list by type and attribute |
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
