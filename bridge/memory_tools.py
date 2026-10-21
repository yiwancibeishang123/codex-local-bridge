"""Explicit project memory and sanitized handoff MCP contracts."""
from pydantic import BaseModel, ConfigDict
from mcp.types import ToolAnnotations
from .memory import MemoryStore
from .handoff import build_project_handoff, build_task_receipt


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


class HandoffMemory(MemoryContract):
    source_id: str
    revision: int
    title: str
    body: str


class ProjectHandoff(MemoryContract):
    project_id: str
    warning: str
    memories: list[HandoffMemory]
    truncated: bool


class TaskReceipt(MemoryContract):
    task_id: str
    project_id: str
    state: str
    execution_succeeded: bool | None
    tests_passed: bool | None
    changes_applied: bool | None
    files_changed: list[str]
    proposal_verified: bool
    summary: str


def register_memory_tools(server, bridge):
    """Explicit retrieval and saving; no automatic transcript capture/injection."""
    storage = MemoryStore(bridge.store.directory, lambda pid: bridge.project(pid))
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

    @server.tool(annotations=read)
    def search_project_memories(project_id: str, query: str = '', limit: int = 20) -> MemorySearchResult:
        """Search explicitly saved notes in an authorized project."""
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
        """Save only user-requested project facts; never secrets or transcripts."""
        note = storage.upsert_note(
            project_id, title=title, body=body, kind=kind, tags=tags,
            origin='chatgpt', source=source, provenance=provenance,
            note_id=note_id, expected_revision=expected_revision)
        bridge.store.audit('project_memory_saved', {
            'project': project_id, 'note_id': note['id'], 'revision': note['revision']})
        return MemorySavedResult(note=MemoryNote(**note))

    @server.tool(annotations=write)
    def archive_project_memory(project_id: str, note_id: str, expected_revision: int) -> MemorySavedResult:
        """Archive one version-checked memory without deleting it."""
        note = storage.archive_note(project_id, note_id, expected_revision=expected_revision)
        bridge.store.audit('project_memory_archived', {
            'project': project_id, 'note_id': note['id'], 'revision': note['revision']})
        return MemorySavedResult(note=MemoryNote(**note))

    @server.tool(annotations=read, structured_output=True)
    def get_project_handoff(project_id: str, query: str = '', limit: int = 8) -> ProjectHandoff:
        """Explicit bounded untrusted memory data; verify against current sources."""
        return ProjectHandoff(**build_project_handoff(bridge, project_id, query, limit))

    @server.tool(annotations=read, structured_output=True)
    def get_task_receipt(task_id: str) -> TaskReceipt:
        """Read actual persisted status and verified proposal paths without logs."""
        return TaskReceipt(**build_task_receipt(bridge, task_id))
