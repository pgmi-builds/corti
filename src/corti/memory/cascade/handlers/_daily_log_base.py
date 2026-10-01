"""Shared diff / dispatch loop for every daily-log cascade handler.

The 3 daily-log kinds (episode / atomic_fact / foresight)
all do the same three-way reconcile against Postgres:

1. Parse the md into structured entries.
2. Fetch the content hash (and vector presence) for the same ``md_path``.
3. ``content_sha256`` mismatch → tokenise + embed + upsert; no diff
   → skip; row gone from md → delete.

The hash covers **only content-bearing fields** declared by each
subclass in :attr:`content_change_keys` (a tuple of ``"section:Name"``
/ ``"inline:name"`` strings). Audit inline fields (owner_id /
session_id / timestamp / parent_id / sender_ids) are NOT in the hash
— editing them does NOT propagate to Postgres and does NOT waste an
embed call.

Embedding is **best-effort**. When the provider is unreachable the row is
still written, with ``vector = NULL``: the memory stays readable and
BM25-searchable, and a later pass backfills the vector. Failing the whole
file instead would lose the record *and* — because the row then never
reaches a terminal state — re-run the handler on every sweep.

Subclasses bind their ``kind`` / ``db_repo`` / ``content_change_keys``
as ClassVars and override :meth:`_embed_texts` + :meth:`_build_row` to do
the per-kind mapping. Everything else — read, diff, embed call, upsert,
delete — lives here.
"""

from __future__ import annotations

import abc
import asyncio
import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from corti.component.embedding import EmbeddingServiceError, EmbedGuard
from corti.core.observability.logging import get_logger
from corti.core.persistence import MarkdownReader, StructuredEntry

from ..types import HandlerOutcome
from ._common import content_sha256 as compute_content_sha256
from ._common import resolve_owner, resolve_scope
from .base import Handler

logger = get_logger(__name__)


# Process-wide gate: every daily-log handler shares one embedding provider.
# The class lives in :mod:`corti.component.embedding` so the read path can
# gate the same provider with its own instance; the name is re-exported here
# for callers that already reach for it through this module.
_embed_guard = EmbedGuard()


@dataclasses.dataclass(frozen=True)
class ParsedEntry:
    """One md-side entry, parsed and digested for diff.

    Held immutable so the diff loop can hash / compare freely.
    """

    entry_id: str
    structured: StructuredEntry
    content_sha256: str


class BaseDailyLogHandler(Handler):
    """Common chassis for the 3 daily-log cascade handlers.

    Subclass requirements:

    - :attr:`kind` (ClassVar[str]) — registry name, surfaces in logs.
    - :attr:`db_repo` (ClassVar) - the Postgres repo singleton for
      this kind (must expose ``find_entry_state`` / ``upsert`` /
      ``delete`` / ``delete_by_md_path``).
    - :attr:`content_change_keys` (ClassVar[tuple[str, ...]]) — the
      subset of inline + section fields whose changes should trigger
      re-upsert + re-embed. Each key is ``"section:Name"`` or
      ``"inline:name"``.
    - :meth:`_embed_texts` (override) — which strings this kind embeds,
      in the order :meth:`_build_row` consumes them.
    - :meth:`_build_row` (override) — turn a :class:`ParsedEntry` plus
      common context (owner_id / owner_type / md_path) into a typed
      Postgres row. Tokenisation lives in the subclass.
    """

    kind: ClassVar[str] = ""
    db_repo: ClassVar[Any] = None
    content_change_keys: ClassVar[tuple[str, ...]] = ()
    # Composite conflict key for the index rows. ``id`` alone is
    # ``<owner_id>_<entry_id>`` and an entry_id is unique only inside one
    # memory space, so the scope has to travel with it — otherwise two
    # app_id/project_id partitions upsert onto each other's rows.
    db_upsert_key: ClassVar[tuple[str, ...]] = ("app_id", "project_id", "id")

    def _content_sha256(self, structured: StructuredEntry) -> str:
        """Hash the content-bearing subset of one entry's inline+sections.

        Walks :attr:`content_change_keys`, projects each key onto its
        ``section:`` / ``inline:`` source on the structured entry, and
        canonicalises into a digest. Unknown key prefixes raise
        :class:`ValueError` so a typo on a subclass surfaces immediately.
        """
        parts: dict[str, str] = {}
        for key in self.content_change_keys:
            kind, _, name = key.partition(":")
            if kind == "section":
                parts[key] = structured.sections.get(name) or ""
            elif kind == "inline":
                parts[key] = structured.inline.get(name) or ""
            else:
                raise ValueError(
                    f"{type(self).__name__}.content_change_keys has unsupported "
                    f"prefix in {key!r}; expected 'section:' or 'inline:'"
                )
        return compute_content_sha256(parts)

    async def handle_added_or_modified(self, md_path: str) -> HandlerOutcome:
        absolute = self._deps.memory_root.root / md_path
        parsed = await MarkdownReader.read(absolute)
        new_entries = [
            ParsedEntry(
                entry_id=entry.id,
                structured=entry.as_structured(),
                content_sha256=self._content_sha256(entry.as_structured()),
            )
            for entry in parsed.entries
        ]

        existing = await self.db_repo.find_entry_state(md_path)
        owner_id, owner_type = resolve_owner(parsed.frontmatter, md_path)
        app_id, project_id = resolve_scope(md_path)

        to_build, skipped = self._diff_entries(new_entries, existing)
        to_upsert = await self._embed_entries(
            to_build,
            owner_id,
            owner_type,
            app_id,
            project_id,
            md_path,
        )
        new_by_id = {e.entry_id for e in new_entries}
        to_delete_ids = [entry_id for entry_id in existing if entry_id not in new_by_id]

        await self._apply_db_changes(to_upsert, to_delete_ids, md_path)
        await self._propagate_deprecations(
            parsed.frontmatter,
            owner_id,
            app_id,
            project_id,
        )
        return HandlerOutcome(
            md_path=md_path,
            kind=self.kind,
            upserted=len(to_upsert),
            deleted=len(to_delete_ids),
            skipped=skipped,
        )

    @staticmethod
    def _diff_entries(
        new_entries: list[ParsedEntry],
        existing: Mapping[str, tuple[str, bool]],
    ) -> tuple[list[ParsedEntry], int]:
        """Compare new entries against existing rows, return changed + skip count.

        ``existing`` maps ``entry_id -> (content_sha256, has_vector)``. A row
        is skipped only when its content hash is unchanged **and** it already
        carries a vector — a row whose vector is still ``NULL`` (written while
        the embedder was down) is re-emitted so the backfill happens on the
        next pass that can reach the provider.
        """
        to_build: list[ParsedEntry] = []
        skipped = 0
        for entry in new_entries:
            prior = existing.get(entry.entry_id)
            if prior is not None:
                prior_sha, prior_has_vector = prior
                if prior_has_vector and prior_sha == entry.content_sha256:
                    skipped += 1
                    continue
            to_build.append(entry)
        return to_build, skipped

    async def _embed_entries(
        self,
        to_build: list[ParsedEntry],
        owner_id: str,
        owner_type: str,
        app_id: str,
        project_id: str,
        md_path: str,
    ) -> list[Any]:
        """Build Postgres rows for changed entries.

        Embedding is batched (one :meth:`embed_batch` call for the whole file
        rather than one request per entry) and best-effort: an unreachable
        provider yields ``vector=None`` rows instead of an exception, so the
        memories are still recorded and keyword-searchable.
        """
        if not to_build:
            return []
        vectors_per_entry = await self._embed_many(to_build, md_path)
        return list(
            await asyncio.gather(
                *(
                    self._build_row(
                        owner_id=owner_id,
                        owner_type=owner_type,
                        app_id=app_id,
                        project_id=project_id,
                        md_path=md_path,
                        entry=entry,
                        vectors=vectors,
                    )
                    for entry, vectors in zip(to_build, vectors_per_entry, strict=True)
                )
            )
        )

    async def _embed_many(
        self,
        entries: list[ParsedEntry],
        md_path: str,
    ) -> list[list[list[float] | None]]:
        """Embed every entry's texts in one batched pass.

        Returns one ``list[vector | None]`` per entry, aligned with
        :meth:`_embed_texts`. Never raises: an unavailable provider yields
        ``None`` for every slot.
        """
        texts_per_entry = [self._embed_texts(entry) for entry in entries]
        flat = [text for texts in texts_per_entry for text in texts]
        if not flat:
            return [[] for _ in entries]

        embedded: list[list[float] | None]
        if _embed_guard.is_open():
            logger.debug(
                "cascade_embedding_guard_open_skipping_embed",
                md_path=md_path,
                texts=len(flat),
            )
            embedded = [None] * len(flat)
        else:
            try:
                embedded = list(await self._deps.embedder.embed_batch(flat))
            except EmbeddingServiceError as exc:
                _embed_guard.record_failure()
                logger.warning(
                    "cascade_embedding_unavailable_keeping_rows_unvectorised",
                    md_path=md_path,
                    entries=len(entries),
                    texts=len(flat),
                    cooldown_seconds=_embed_guard.cooldown_seconds,
                    error=str(exc),
                )
                embedded = [None] * len(flat)
            else:
                _embed_guard.record_success()

        grouped: list[list[list[float] | None]] = []
        cursor = 0
        for texts in texts_per_entry:
            grouped.append(embedded[cursor : cursor + len(texts)])
            cursor += len(texts)
        return grouped

    async def _resolve_vectors(
        self,
        entry: ParsedEntry,
        md_path: str,
        vectors: Sequence[list[float] | None] | None,
    ) -> Sequence[list[float] | None]:
        """Return caller-supplied vectors, or embed this single entry.

        The batched path (:meth:`_embed_entries`) always supplies ``vectors``;
        this fallback keeps :meth:`_build_row` callable on its own (white-box
        tests, one-off tooling) and degrades to ``None`` when the embedder is
        down rather than raising.
        """
        if vectors is not None:
            return vectors
        return (await self._embed_many([entry], md_path))[0]

    async def _apply_db_changes(
        self,
        to_upsert: list[Any],
        to_delete_ids: list[str],
        md_path: str,
    ) -> None:
        """Flush upserts and deletes to Postgres."""
        if to_upsert:
            await self.db_repo.upsert(to_upsert, by=self.db_upsert_key)
        if to_delete_ids:
            in_list = ", ".join(f"'{eid}'" for eid in to_delete_ids)
            await self.db_repo.delete(
                f"md_path = '{_q(md_path)}' AND entry_id IN ({in_list})"
            )

    async def handle_deleted(self, md_path: str) -> HandlerOutcome:
        deleted = await self.db_repo.delete_by_md_path(md_path)
        return HandlerOutcome(
            md_path=md_path,
            kind=self.kind,
            upserted=0,
            deleted=deleted,
            skipped=0,
        )

    async def _propagate_deprecations(
        self,
        frontmatter: Any,
        owner_id: str,
        app_id: str,
        project_id: str,
    ) -> None:
        """Propagate deprecated_entries from frontmatter to Postgres.

        The md file is the source of truth; cascade reconstructs the
        ``deprecated_by`` column on every sync/rebuild.
        """
        deprecated = getattr(frontmatter, "deprecated_entries", None)
        if not deprecated and isinstance(frontmatter, dict):
            deprecated = frontmatter.get("deprecated_entries")
        if not deprecated:
            return
        scope = (app_id, project_id)
        await asyncio.gather(
            *(
                self._mark_deprecated(owner_id, entry_id, deprecated_by_val, scope)
                for entry_id, deprecated_by_val in deprecated.items()
            )
        )

    async def _mark_deprecated(
        self,
        owner_id: str,
        entry_id: str,
        deprecated_by: str,
        scope: tuple[str, str],
    ) -> None:
        """Set ``deprecated_by`` on a Postgres row matching ``entry_id``.

        Scoped to ``(app_id, project_id, owner_id, entry_id)`` to avoid
        cross-space collisions. A missing row is silently ignored — the
        entry may have been deleted or not yet indexed.
        """
        app_id, project_id = scope
        predicate = (
            f"owner_id = '{_q(owner_id)}' "
            f"AND entry_id = '{_q(entry_id)}' "
            f"AND app_id = '{_q(app_id)}' "
            f"AND project_id = '{_q(project_id)}'"
        )
        try:
            await self.db_repo.update(
                {"deprecated_by": deprecated_by},
                where=predicate,
            )
        except Exception:
            logger.warning(
                "failed to mark entry deprecated",
                entry_id=entry_id,
                deprecated_by=deprecated_by,
                kind=self.kind,
                exc_info=True,
            )

    @abc.abstractmethod
    def _embed_texts(self, entry: ParsedEntry) -> tuple[str, ...]:
        """Texts to embed for ``entry``, aligned with :meth:`_build_row`.

        Order matters: the vector at index *i* is handed to ``_build_row``
        as ``vectors[i]``. Return an empty tuple when the kind embeds
        nothing for this entry.
        """

    @abc.abstractmethod
    async def _build_row(
        self,
        *,
        owner_id: str,
        owner_type: str,
        app_id: str = "default",
        project_id: str = "default",
        md_path: str,
        entry: ParsedEntry,
        vectors: Sequence[list[float] | None] | None = None,
    ) -> Any:
        """Subclass: build the typed Postgres row for one parsed entry.

        ``app_id`` / ``project_id`` carry the path-derived scope; the base
        always supplies them (via :func:`resolve_scope`). They default to
        ``"default"`` so white-box callers exercising only the field mapping
        can omit them.

        ``vectors`` is the pre-computed embedding output aligned with
        :meth:`_embed_texts`; a ``None`` slot means "not embedded" and must be
        written as SQL ``NULL``. When ``vectors`` is ``None`` the entry is
        embedded on demand — subclasses should route that through
        :meth:`_resolve_vectors`.
        """


def _q(text: str) -> str:
    """Defensive SQL-quote escape (mirrors postgres chassis convention)."""
    return text.replace("'", "''")
