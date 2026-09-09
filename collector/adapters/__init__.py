"""Source adapter framework (docs/design/regradar-architecture.md section 7).

Importing this package must never require optional adapter dependencies
(e.g. playwright) to be installed - each adapter module guards its own
heavy import so `collector.adapters.registry` stays importable everywhere.
"""
