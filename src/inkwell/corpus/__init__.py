"""The research corpus: material that exists before the question does.

Every other research surface in this project is a search box — a query goes
out and whatever it happened to name comes back. This package builds the other
half: an enumerated, stored corpus the writer reads with Grep and Read, so
coverage is what a source published rather than what an agent thought to ask.

- ``registry`` declares each tracked source once — hosts, avenues, venue,
  authority, organization.
- ``discovery`` enumerates what a source published, per avenue.
- ``storage`` holds documents as files beside one typed JSON index per source.
- ``quality`` scores a capture against declared, overridable rules.
- ``fetch`` reaches a page, escalating to a browser only where declared.
- ``ingest`` runs the whole loop and reports what it did, per source.
"""
