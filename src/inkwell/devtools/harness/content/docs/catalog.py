"""Every document inkwell publishes under ``docs/``.

The pages about lup's own machinery come from the library, because inkwell is
built on that machinery and its guidance points at those pages by name.
Declared here is the one page whose subject is inkwell itself. Generation
turns the roster into artifacts, so a document not declared here does not
exist and a file under ``docs/`` produced from nowhere is deleted as unowned.
Nothing beneath ``docs/`` is hand-written.
"""

import lup.harness.models as models
from lup.adapters.claude.harness import CLAUDE_DISPATCHER
from lup.adapters.codex.harness import CODEX_DISPATCHER
from lup.devtools.harness.content.docs.catalog import library_documents, published
from inkwell.devtools.harness.content.catalog import AGENTS, PLUGIN_NAME, SKILLS
from inkwell.devtools.harness.content.docs import inkwell

DOCS_ROOT = "src/inkwell/devtools/harness/content/docs"
"""Directory every project-owned document's canonical module lives in."""

LIBRARY_DOCS_ROOT = "lup/devtools/harness/content/docs"
"""Where the library's own document modules live, as a banner reads them.

Lup is a git dependency rather than a checkout in this tree, so the default
`packages/lup/...` provenance would name a path no reader here can open. The
module path inside the installed package is what they can actually resolve.
"""

REFERENCE = library_documents(
    SKILLS,
    AGENTS,
    PLUGIN_NAME,
    CLAUDE_DISPATCHER.routed_tools,
    CODEX_DISPATCHER.routed_tools,
    LIBRARY_DOCS_ROOT,
)
"""The pages lup publishes about the machinery inkwell is built on.

The parity audit reads what each runtime decodes from the runtime itself, so
composing them is what this root is for: the page stays portable while the
table it publishes cannot claim a decoded set that stopped being true. Codex
appears there as the runtime lup differentiates against, not as one inkwell
generates a tree for — `NATIVE_RUNTIMES` is what decides that.
"""

DOCUMENTS: list[models.Document] = [
    published("inkwell", "inkwell.md", inkwell.DOCUMENT, DOCS_ROOT),
    *REFERENCE,
]
"""Every document under ``docs/``, inkwell's own first."""
