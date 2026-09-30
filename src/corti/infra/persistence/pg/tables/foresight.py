"""PG table schema for foresight."""

from __future__ import annotations

import datetime as dt
from typing import ClassVar

from corti.infra.persistence.pg.base import PgBaseModel

from ._parent_type import ParentType


class Foresight(PgBaseModel):
    """PG schema for ``foresight`` table."""

    TABLE_NAME: ClassVar[str] = "foresight"
    BM25_FIELDS: ClassVar[list[str]] = ["foresight_tokens", "evidence_tokens"]

    id: str
    entry_id: str
    owner_id: str
    owner_type: str
    app_id: str = "default"
    project_id: str = "default"
    session_id: str | None = None
    timestamp: dt.datetime
    start_time: dt.datetime | None = None
    end_time: dt.datetime | None = None
    duration_days: int | None = None
    parent_type: str = ParentType.MEMCELL.value
    parent_id: str
    sender_ids: list[str] = []
    foresight: str
    foresight_tokens: str = ""
    evidence: str | None = None
    evidence_tokens: str | None = None
    md_path: str
    content_sha256: str
    vector: list[float] | None = None
