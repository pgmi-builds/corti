"""PG table schema for episode."""

from __future__ import annotations

import datetime as dt
from typing import ClassVar

from corti.infra.persistence.pg.base import PgBaseModel

from ._parent_type import ParentType


class Episode(PgBaseModel):
    """PG schema for ``episode`` table."""

    TABLE_NAME: ClassVar[str] = "episode"
    BM25_FIELDS: ClassVar[list[str]] = ["episode_tokens"]

    id: str
    entry_id: str
    owner_id: str
    owner_type: str
    app_id: str = "default"
    project_id: str = "default"
    session_id: str | None = None
    timestamp: dt.datetime
    parent_type: str = ParentType.MEMCELL.value
    parent_id: str
    sender_ids: list[str] = []
    subject: str | None = None
    summary: str | None = None
    episode: str
    episode_tokens: str = ""
    md_path: str
    content_sha256: str
    deprecated_by: str | None = None
    vector: list[float] | None = None
    subject_vector: list[float] | None = None
