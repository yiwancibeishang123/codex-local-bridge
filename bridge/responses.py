"""Explicit, flat MCP file-tool contracts for structured-content consumers.

FastMCP does not infer structured output from a bare ``dict`` annotation.
Use named fields rather than relying on clients parsing a JSON text block.
"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_serializer


class ResponseModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class FileReadResult(ResponseModel):
    project_id: str
    path: str = Field(description='Authorized project-relative path; never an absolute host path')
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$', description='SHA-256 of the original file bytes, before redaction')
    bytes: int = Field(ge=0, description='Original UTF-8 file size in bytes')
    content: str = Field(description='UTF-8 text, with known credential patterns redacted if necessary')
    redacted: bool


class FileEntry(ResponseModel):
    path: str | None = None
    bytes: int | None = Field(default=None, ge=0)
    directory: str | None = None

    @model_serializer(mode='wrap')
    def omit_unused_fields(self, handler):
        # Keep the original listing shape: directories have no `path` key.
        return {key:value for key,value in handler(self).items() if value is not None}


class FileListResult(ResponseModel):
    project_id: str
    directory: str
    entries: list[FileEntry]
    limit: int


class FileWriteResult(ResponseModel):
    proposal_id: str
    status: Literal['pending_local_approval', 'applied']
    project_id: str
    path: str
    old_sha256: str
    new_sha256: str
    diff: str
    diff_redacted: bool
    diff_truncated: bool
    next_step: str | None = None
    authorization: str | None = None
    grant_id: str | None = None


class FileWriteStatus(ResponseModel):
    id: str
    project_id: str
    path: str
    status: str
    created_at: int
    applied_at: int | None = None
