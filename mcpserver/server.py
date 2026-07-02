import json
import logging
import os

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from collector.storage import init_db
from mcpserver.queries import (
    get_item,
    get_recent,
    get_run_log,
    get_warnings,
    list_regulators,
    search_items,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

app = Server("casp-radar")

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        db_path = os.getenv("DB_PATH", "/app/data/casp_radar.db")
        _engine = init_db(db_path)
    return _engine


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_items",
            description="Full-text search across regulatory items by title and snippet.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "regulator_id": {"type": "string"},
                    "language": {"type": "string", "description": "BCP-47 code e.g. 'en', 'de'"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "date_from": {"type": "string", "description": "ISO date e.g. 2026-01-01"},
                    "date_to": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_recent",
            description="Returns the most recent regulatory items, with optional filters.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 20},
                    "regulator_id": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "language": {"type": "string"},
                },
            },
        ),
        Tool(
            name="list_regulators",
            description="Returns all regulators in the registry with live fetch stats.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_item",
            description="Returns a single item by its ID, including raw_content.",
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        ),
        Tool(
            name="get_run_log",
            description="Returns recent collector run statistics.",
            inputSchema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 10}},
            },
        ),
        Tool(
            name="get_warnings",
            description="Returns regulatory warning items across all regulators. days=0 returns all.",
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "default": 30, "description": "0 = all; max 365"},
                    "regulator_id": {"type": "string"},
                },
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict):
    engine = get_engine()

    if name == "search_items":
        results = search_items(
            engine,
            query=arguments["query"],
            regulator_id=arguments.get("regulator_id"),
            language=arguments.get("language"),
            tags=arguments.get("tags"),
            date_from=arguments.get("date_from"),
            date_to=arguments.get("date_to"),
            limit=arguments.get("limit", 20),
        )
    elif name == "get_recent":
        results = get_recent(
            engine,
            limit=arguments.get("limit", 20),
            regulator_id=arguments.get("regulator_id"),
            tags=arguments.get("tags"),
            language=arguments.get("language"),
        )
    elif name == "list_regulators":
        results = list_regulators(engine)
    elif name == "get_item":
        results = get_item(engine, arguments["id"])
    elif name == "get_run_log":
        results = get_run_log(engine, limit=arguments.get("limit", 10))
    elif name == "get_warnings":
        results = get_warnings(
            engine,
            days=arguments.get("days", 30),
            regulator_id=arguments.get("regulator_id"),
        )
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    return [TextContent(type="text", text=json.dumps(results, default=str))]


async def main():
    async with stdio_server() as streams:
        await app.run(streams[0], streams[1], app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
