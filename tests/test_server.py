import importlib

import pytest
from sqlalchemy import text


def _fresh_server_module():
    import mcpserver.server as server
    importlib.reload(server)
    return server


def _server_with_db(tmp_path, monkeypatch):
    """Creates a real on-disk DB via the collector's init_db (read-write),
    points DB_PATH at it, and returns (server, write_engine): server is a
    freshly-reloaded mcpserver.server module whose get_engine() opens that
    same file read-only - mirroring how the collector and MCP server share
    a DB file in production.
    """
    from collector.storage import init_db
    db_path = tmp_path / "test.db"
    write_engine = init_db(str(db_path))
    monkeypatch.setenv("DB_PATH", str(db_path))
    server = _fresh_server_module()
    return server, write_engine


def test_get_engine_exits_if_db_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "missing.db"))
    server = _fresh_server_module()
    with pytest.raises(SystemExit):
        server.get_engine()


def test_get_engine_opens_existing_db_read_only_and_rejects_writes(tmp_path, monkeypatch):
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    engine = server.get_engine()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM items")).scalar() == 0
    with pytest.raises(Exception):
        with engine.connect() as conn:
            conn.execute(text("INSERT INTO regulators (id, name) VALUES ('x', 'x')"))
            conn.commit()


def test_transport_security_enabled_with_allowed_hosts():
    # FastMCP only auto-enables DNS-rebinding protection when host is left
    # at 127.0.0.1/localhost/::1 - this project binds "0.0.0.0" (needed for
    # Docker networking), which silently falls through to protection
    # disabled unless configured explicitly.
    server = _fresh_server_module()
    ts = server.mcp.settings.transport_security
    assert ts.enable_dns_rebinding_protection is True
    assert "127.0.0.1:8000" in ts.allowed_hosts
