"""Private stdio MCP adapter for Codex sessions launched outside Bridge.

Opt-in, local only: no OAuth HTTP listener, background process, worker,
project writes or host commands. Uses exactly the same project-scoped SQLite
memory DB as ChatGPT's Bridge MCP and the Qt desktop app.
"""
import argparse
import json
import os
from pathlib import Path

from .cli import load_config, config_path
from .core import Bridge
from .memory import MemoryStore
from .handoff import build_project_handoff, build_task_receipt


class LocalCodexMemory:
    def __init__(self, config):
        self.bridge = Bridge(config)
        self.storage = MemoryStore(self.bridge.store.directory, self.bridge.project)

    def search(self, project_id, query="", limit=12):
        if not isinstance(query, str):
            raise ValueError("Invalid query")
        if type(limit) is not int or not 1 <= limit <= 25:
            raise ValueError("Limit must be 1..25")
        items = (self.storage.search_notes(project_id, query, limit=limit)
                 if query.strip() else self.storage.list_notes(project_id, limit=limit))
        return {"project_id": project_id, "query": query, "notes": items}

    def save(self, project_id, title, body, kind="knowledge", tags=None):
        # The authenticated local Codex process explicitly invokes this tool.
        # It does not scrape independent conversation logs behind the user's back.
        item = self.storage.upsert_note(
            project_id, title=title, body=body, kind=kind,
            tags=tags, origin="codex", source="codex-local-stdio",
            provenance="Independent local Codex session; model-provided, verify before trusting")
        self.bridge.store.audit("codex_local_memory_saved",
                                {"project": project_id, "note_id": item["id"]})
        return {"note": item}

    def handoff(self, project_id, query="", limit=8):
        return build_project_handoff(self.bridge, project_id, query, limit=limit)

    def receipt(self, task_id):
        return build_task_receipt(self.bridge, task_id)


def make_server(config):
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations
    api = LocalCodexMemory(config)
    mcp = FastMCP("Bridge Studio Project Memory")
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    edit = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

    @mcp.tool(annotations=readonly)
    def search_bridge_project_memories(project_id: str, query: str = "", limit: int = 12) -> dict:
        """Search the same local project memory database as ChatGPT MCP."""
        return api.search(project_id, query, limit)

    @mcp.tool(annotations=edit)
    def save_bridge_project_memory(project_id: str, title: str, body: str,
                                   kind: str = "knowledge", tags: list[str] | None = None) -> dict:
        """Explicitly save a verified result or decision (never raw secrets/transcripts)."""
        return api.save(project_id, title, body, kind, tags)

    @mcp.tool(annotations=readonly)
    def get_bridge_project_handoff(project_id: str, query: str = "", limit: int = 8) -> dict:
        """Bounded untrusted historical notes for continuity; verify against files."""
        return api.handoff(project_id, query, limit)

    @mcp.tool(annotations=readonly)
    def get_bridge_task_receipt(task_id: str) -> dict:
        """Get verified persisted task state without raw logs or prompts."""
        return api.receipt(task_id)

    return mcp


def toml_snippet(python, config):
    # JSON quoted strings are also valid TOML basic string syntax for simple paths.
    return (
        "# Add manually to ~/.codex/config.toml only after reviewing this block.\n"
        "[mcp_servers.bridge_memory]\n"
        "command = " + json.dumps(str(python)) + "\n"
        "args = [\"-m\", \"bridge.codex_memory_stdio\"]\n"
        "env = { BRIDGE_CONFIG = " + json.dumps(str(config)) + " }\n"
    )


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print-config", action="store_true",
                        help="Print a safe example for local Codex MCP registration")
    args = parser.parse_args(argv)
    config = load_config()  # requires existing owner-private BRIDGE_CONFIG
    if args.print_config:
        import sys
        print(toml_snippet(Path(sys.executable).resolve(), config_path().resolve()))
        return
    make_server(config).run(transport="stdio")

if __name__ == "__main__":
    main()
