"""Source adapter framework (../../wiki/content/concept-source-adapter-framework.md).

Importing this package must never require optional adapter dependencies
(e.g. playwright) to be installed - each adapter module guards its own
heavy import so `collector.adapters.registry` stays importable everywhere.
"""
