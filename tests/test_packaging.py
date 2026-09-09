"""Regression test: the installed `regradar` console script must be able to
import its dependencies (collector, mcpserver, webui) when run outside the
repo. See docs/design/regradar-architecture.md §6a.
"""
from importlib.metadata import entry_points


def test_regradar_console_script_loads():
    (ep,) = [e for e in entry_points(group="console_scripts") if e.name == "regradar"]
    main = ep.load()
    assert callable(main)
