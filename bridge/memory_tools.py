"""Explicit project-scoped shared memory MCP tools.

The caller must already hold the Bridge OAuth connection. A memory note is an
owner-approved project artifact, NOT a dump of private ChatGPT conversations.
Results use Pydantic models so MCP outputSchema/structuredContent are populated.
"""
from pydantic import BaseModel, ConfigDict, Field
from mcp.types import ToolAnnotations
from .memory import MemoryStore


class MemoryContract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class MemoryNote(MemoryContract):
    id: str
    project_id: str
    title: str
    body: str
    kind: str
    tags: list[str]
    origin: str
    source: str
    provenance: str
    created_at: float
    updated_at: float
    revision: int
    archived: bool


class MemorySearchResult(MemoryContract):
    project_id: str
    query: str
    notes: list[MemoryNote]


class MemorySavedResult(MemoryContract):
    note: MemoryNote


def register_memory_tools(server, bridge):
    """Register *explicit* shared memory tools; no automatic chat transcript capture."""
    storage = MemoryStore(bridge.store.directory, lambda pid: bridge.project(pid))
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

    @server.tool(annotations=read)
    def search_project_memories(project_id: str, query: str = '', limit: int = 20) -> MemorySearchResult:
        """Search notes for an authorized project, or list recent notes with an empty query.

        Include relevant results explicitly in a Codex delegation prompt when
        needed; saving notes is an independent action that the user can inspect.
        """
        if not isinstance(query, str):
            raise ValueError('Invalid memory query')
        notes = storage.search_notes(project_id, query, limit=limit) if query.strip() else storage.list_notes(project_id, limit=limit)
        return MemorySearchResult(project_id=project_id, query=query, notes=[MemoryNote(**n) for n in notes])

    @server.tool(annotations=write)
    def save_project_memory(project_id: str, title: str, body: str,
                            kind: str = 'knowledge', tags: list[str] | None = None,
                            source: str = 'chatgpt-web', provenance: str = '',
                            note_id: str | None = None,
                            expected_revision: int | None = None) -> MemorySavedResult:
        """Explicitly create or revision-check update of a scoped long-term project memory.

        Use only when the user requests remembering/saving a project fact. Do not
        pass secrets, credentials, or unreviewed private conversation transcripts.
        """
        note = storage.upsert_note(
            project_id, title=title, body=body, kind=kind, tags=tags,
            origin='chatgpt', source=source, provenance=provenance,
            note_id=note_id, expected_revision=expected_revision)
        bridge.store.audit('project_memory_saved', {
            'project': project_id, 'note_id': note['id'], 'revision': note['revision']})
        return MemorySavedResult(note=MemoryNote(**note))

    @server.tool(annotations=write)
    def archive_project_memory(project_id: str, note_id: str, expected_revision: int) -> MemorySavedResult:
        """Archive one version-checked memory (soft delete; data remains on disk)."""
        note = storage.archive_note(project_id, note_id, expected_revision=expected_revision)
        bridge.store.audit('project_memory_archived', {
            'project': project_id, 'note_id': note['id'], 'revision': note['revision']})
        return MemorySavedResult(note=MemoryNote(**note))
