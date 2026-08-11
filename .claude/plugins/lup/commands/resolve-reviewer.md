---
description: "Review one resolver concern against its acceptance criteria"
---

Independently review the supplied concern commit against every persisted acceptance criterion. Inspect the complete diff, reject omissions and scope leaks, and return the typed review report without editing. Where a criterion asks for a `# lup: solved: <text>` claim, the claim standing in the diff is that criterion met — the orchestrator strips a concern's open feedback before the worker starts, so the marker is a record of the answer and not feedback re-opened.
