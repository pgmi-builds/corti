# Corti HTTP API (v1)

Human-readable reference for the Corti HTTP API. Schema names, types
and validation constraints mirror the OpenAPI spec served at
`GET /openapi.json` (when the server runs with `ENV=DEV`; see
[OpenAPI spec source](#openapi-spec-source)). This document adds the
business semantics the raw spec does not carry.

## Table of contents

- [Overview](#overview)
  - [Base URL and versioning](#base-url-and-versioning)
  - [Content type](#content-type)
  - [Authentication](#authentication)
  - [Response envelope](#response-envelope)
  - [Eventual consistency](#eventual-consistency)
  - [Conventions](#conventions)
  - [ScopeId: `app_id` and `project_id`](#scopeid-app_id-and-project_id)
- [Errors](#errors)
- [Common types](#common-types)
  - [MessageItem](#messageitem)
  - [ContentItem](#contentitem)
  - [ToolCall](#toolcall)
  - [ToolFunction](#toolfunction)
  - [FilterNode (filter DSL)](#filternode-filter-dsl)
  - [SearchMethod](#searchmethod)
  - [GetMemoryType](#getmemorytype)
- [Endpoints](#endpoints)
  - [POST /api/v1/memory/add](#post-apiv1memoryadd)
  - [POST /api/v1/memory/flush](#post-apiv1memoryflush)
  - [POST /api/v1/memory/search](#post-apiv1memorysearch)
  - [POST /api/v1/memory/get](#post-apiv1memoryget)
  - [Runtime interop endpoints](#runtime-interop-endpoints)
    - [POST /api/v1/memory/session/start](#post-apiv1memorysessionstart)
    - [POST /api/v1/memory/prefetch](#post-apiv1memoryprefetch)
    - [POST /api/v1/memory/session/end](#post-apiv1memorysessionend)
    - [Degradation reporting](#degradation-reporting)
  - [POST /api/v1/ome/trigger](#post-apiv1ometrigger)
  - [Knowledge endpoints](#knowledge-endpoints)
- [OpenAPI spec source](#openapi-spec-source)

## Overview

### Base URL and versioning

| Setting | Default | Override |
|---|---|---|
| Host | `127.0.0.1` (loopback only) | `CORTI_API__HOST` env var or `--host` flag |
| Port | `8000` | `CORTI_API__PORT` env var or `--port` flag |
| Version prefix | `/api/v1` | — |

Business endpoints live under `/api/v1/memory/`, `/api/v1/ome/`, and
`/api/v1/knowledge/`. Knowledge endpoints have their own dedicated
reference at [docs/knowledge.md](knowledge.md) and are cross-referenced
below. The operational endpoints `GET /health` and `GET /metrics` exist
but are intentionally outside this reference — they are runtime probes
for deployment, not part of the application contract.

### Content type

All `POST` endpoints require `Content-Type: application/json`. Request
and response bodies are UTF-8 JSON.

### Authentication

Corti ships **no built-in authentication**. The server binds to
`127.0.0.1` by default; place your own gateway or auth layer in front
before exposing the API on any other interface.

### Response envelope

Successful (`200 OK`) responses always wrap the payload in:

```json
{
  "request_id": "<32-char hex>",
  "data": { /* endpoint-specific payload */ }
}
```

`request_id` is generated server-side (32 lowercase hex chars) and is
echoed by structured logs / tracing / metrics, so a single request can
be correlated end-to-end. Error responses carry their own envelope —
`request_id` at the top level alongside a nested `error` object (not a
bare FastAPI `detail`); see [Errors](#errors).

### Eventual consistency

`/add` and `/flush` are **async-accept** endpoints: they durably store
the raw messages (SQLite `unprocessed_buffer` rows) and return
immediately with `status: "accepted"`. The expensive work — boundary
detection (an LLM call), memory-cell carving, the user / agent
extraction pipelines, and the markdown writes — runs afterwards on the
server's per-session worker queue, strictly FIFO per `session_id`.

That means `/search` and `/get` may not see a record immediately after
the `/add` or `/flush` that produced it. Typical extraction latency is
a few seconds, but under load or slow LLM it can reach 30–60 seconds.
If you need read-your-write semantics, retry with backoff. The
`unprocessed_buffer` rows are durable from the moment the call returns;
extraction failure or a server restart never loses data (a boot-time
sweep re-queues any session that still has buffered rows).

### Conventions

#### Timestamps

Corti runs a **two-zone discipline**: every stored byte is UTC; every
rendered value carries the configured **display timezone**. The
display timezone is set by `CORTI_MEMORY__TIMEZONE` (env var) or
`[memory] timezone` (TOML); default `"UTC"`.

| Direction | Format | Notes |
|---|---|---|
| **Request** — `messages[].timestamp` (`/add`) | Integer, **Unix epoch milliseconds (ms)** | Server auto-detects `>= 10^12` as ms for backward compat, but the contract is ms |
| **Request** — `filters.timestamp.{gte,lt,…}` (`/search` / `/get`) | Integer, **Unix epoch ms** *or* ISO-8601 string | Same auto-detect; pick whichever you have |
| **Response** — every `timestamp` field | ISO-8601 string with **explicit timezone offset** (`+HH:MM` or `Z`) | Always rendered in the configured display tz; never naive |

With the default `CORTI_MEMORY__TIMEZONE=UTC` an episode produced
at 11:30 UTC renders as `"2026-05-28T11:30:36Z"` (Pydantic
canonicalises `timezone.utc` to `Z`). Switch to
`CORTI_MEMORY__TIMEZONE=Asia/Shanghai` and the same UTC instant
renders as `"2026-05-28T19:30:36+08:00"` — bytes on disk are
unchanged.

**Fallback for naive input.** If you submit a `filters.timestamp` ISO
string without an offset (e.g. `"2026-05-28T11:30:00"`), the server
treats it as already display-tz-local before comparing against
storage. This is the same rule users see when reading rendered output:
"if you didn't say a zone, we assume your zone."

**For internal architecture.** The storage / display split lives in
`corti.component.utils.datetime`
(`get_utc_now` / `ensure_utc` / `UtcDatetime` for storage;
`get_now_with_timezone` / `to_display_tz` for display). See
[datetime.md](datetime.md) for the design rationale.

#### Other conventions

- **Server-generated IDs** follow `<owner>_<kind>_<YYYYMMDD>_<NNN>`,
  e.g. `alice_ep_20260528_00000001` for an episode, `alice_af_...`
  for an atomic fact. See
  [storage_layout.md §4](storage_layout.md) for the encoding.
- **All endpoints are POST** for `/api/v1/memory/*` even when the
  semantics look like a read (`/search`, `/get`) — the request bodies
  are too rich (filters, methods, paging) to encode in a query string.

### ScopeId: `app_id` and `project_id`

`app_id` and `project_id` partition memory at the disk layer. Every
write lands under
`~/.corti/<app>/<project>/users/<user_id>/...` (or `.../agents/...`
for the agent track). The default scope materialises on disk as
`default_app` / `default_project` (the `_app` / `_project` suffix is
added only for the literal id `"default"` so the default space stays
visually distinct from user-named scopes).

A `/search` or `/get` query never crosses scopes — different
`(app_id, project_id)` pairs are isolated.

Both fields share the same validation:

| Setting | Value |
|---|---|
| Type | `string` |
| Default | `"default"` |
| Length | 1–128 chars |
| Charset | `^[a-zA-Z0-9_.-]+$` |
| Rejected literals | `"."` and `".."` (path-traversal guard) |

## Errors

Every non-2xx response uses a uniform error envelope — `request_id` at
the top level (mirroring the success envelope) alongside a nested
`error` object. It is **not** a bare FastAPI `detail`:

```json
{
  "request_id": "<32-char hex>",
  "error": {
    "code": "NOT_FOUND",
    "message": "Document 'abc123' not found",
    "timestamp": "2026-06-01T12:24:46+00:00",
    "path": "/api/v1/knowledge/documents/abc123"
  }
}
```

### error.code values

`error.code` is a machine-readable `ErrorCode` enum. Clients can switch
on this value to decide retry / display / routing behaviour without
parsing the human-readable `message` field.

| `error.code` | HTTP | Retryable? | When |
|---|---|---|---|
| `NOT_FOUND` | `404` | No | Requested resource does not exist |
| `CONFLICT` | `409` | No | Operation conflicts with existing state (e.g. duplicate document) |
| `INVALID_INPUT` | `422` | No | Request-body validation failure. Also covers `/search` / `/get` filter-DSL compile errors — the compile reason rides in `message` |
| `EXTRACTION_EMPTY` | `422` | No | Document extraction produced no topics (empty or whitespace-only content) |
| `BAD_REQUEST` | `400` | No | Path traversal attempt or other malformed input |
| `UNSUPPORTED_FORMAT` | `415` | No | File format or modality not supported (e.g. unsupported `ContentItem` type, missing `ext` for `base64`) |
| `EXTERNAL_SERVICE_UNAVAILABLE` | `503` | **Yes** | An external service (LLM, embedding, rerank) returned an error or timed out |
| `CAPABILITY_UNAVAILABLE` | `503` | No | A required server-side capability is missing (e.g. `corti[multimodal]` extra not installed, LibreOffice absent) — requires admin action, not retry |
| `CONFIGURATION_ERROR` | `500` | No | A required configuration is missing or invalid (e.g. embedding model not set) |
| `INTERNAL_ERROR` | `500` | No | Unhandled exception (internal details are logged, never leaked) |

### error object

| Field | Type | Description |
|---|---|---|
| `code` | `string` | One of the `ErrorCode` values listed above |
| `message` | `string` | Human-readable reason. For `INVALID_INPUT` from request validation, **only the first** validation error is surfaced, formatted `"<msg>: <dotted-loc>"` with the leading `body` segment stripped (e.g. `"Field required: messages"`); a model-level validator with no field location surfaces just `"<msg>"` (e.g. `"Value error, exactly one of user_id / agent_id must be provided"`) |
| `timestamp` | `string` | ISO-8601 with timezone offset (display tz) |
| `path` | `string` | Request path, e.g. `/api/v1/memory/add` |

> Unlike FastAPI's default, the full per-field validation array is **not**
> returned — only the first error's message. A client that needs the
> offending field can read the `<loc>` suffix in `message`.

## Common types

### MessageItem

One turn in a `/add` batch. Shape mirrors the OpenAI Chat Completions
message structure plus a stable `sender_id` for indexing.

| Field | Type | Required | Default | Constraints |
|---|---|---|---|---|
| `sender_id` | `string` | yes | — | `minLength=1` |
| `sender_name` | `string \| null` | no | `null` | — |
| `role` | `"user" \| "assistant" \| "tool"` | yes | — | — |
| `timestamp` | `integer` | yes | — | `> 0` — **Unix epoch milliseconds (ms)** per v1 contract |
| `content` | `string \| array<ContentItem>` | yes | — | — |
| `tool_calls` | `array<ToolCall> \| null` | no | `null` | — |
| `tool_call_id` | `string \| null` | no | `null` | — |

**`sender_id`** — Stable identifier of the entity producing this turn.
For `role: "user"`, this is the `user_id` the server will index the
extracted memory under: markdown lands at
`users/<sender_id>/episodes/...`, and `/search` / `/get` queries with
`user_id: "<sender_id>"` reach it. For `role: "assistant"` or
`"tool"`, the field is informational (the LLM sees it during
extraction but it is not used as an indexing key).

**`sender_name`** — Optional human-readable display name. Lets the LLM
use a real name during extraction without changing the indexing key.
For example, set `sender_id: "u_42"` (stable internal id) and
`sender_name: "Alice"` (what the LLM sees).

**`role`** — Speaker role; one of:
- `"user"` — content originates from a human (or human-acting client).
- `"assistant"` — content from the AI assistant.
- `"tool"` — output of a tool call; pair with `tool_call_id`.

**`timestamp`** — Wall-clock anchor in **Unix epoch milliseconds (ms)**
per the v1 API contract. Used by extraction to anchor the resulting
episode and by the daily-log writer to bucket the entry into the right
file (`episode-<YYYY-MM-DD>.md` etc.).

> Implementation note: the algo layer auto-detects seconds vs ms (values
> `>= 10^12` are treated as ms, smaller as seconds) for backward compat,
> but **clients should send ms** to honour the contract.

**`content`** — The message body.
- A bare **string** is shorthand for a single text content item.
- An **array of `ContentItem`** is for mixed-modality input (text +
  image / pdf / audio / ...); non-text items are parsed by the
  multimodal LLM configured via `CORTI_MULTIMODAL__*` env vars. See
  [ContentItem](#contentitem).

**`tool_calls`** — When `role: "assistant"`, the tool calls the
assistant emitted in this turn (OpenAI Chat Completions shape).

**`tool_call_id`** — When `role: "tool"`, the `id` of the call this
message is the response to.

### ContentItem

Mixed-modality message-body element. Carry the payload in exactly one
of `text` / `uri` / `base64`; the others must be `null`. For
`type: "text"` use `text`; for every **non-text** type use `uri`
(`http(s)://`) or `base64` (with `ext`). Non-text items are routed
through the multimodal parser, which needs a fetchable or decodable
payload — a non-text item carrying only `text` returns `415`.

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `type` | `"text" \| "image" \| "audio" \| "doc" \| "pdf" \| "html" \| "email"` | yes | — | — |
| `text` | `string \| null` | no | `null` | Required when `type: "text"` |
| `uri` | `string \| null` | no | `null` | `http(s)://` (fetched server-side) or `file://` (read from the server's local fs, guardrailed) pointer |
| `base64` | `string \| null` | no | `null` | Inline payload, plain base64 (no `data:` prefix) |
| `ext` | `string \| null` | no | `null` | File-extension hint when `uri` lacks one |
| `name` | `string \| null` | no | `null` | Display filename, used in logs |
| `extras` | `object \| null` | no | `null` | Provider-specific metadata, opaque to Corti |

**`type`** — The content kind. Each non-text type is dispatched to the
multimodal LLM. If the multimodal endpoint cannot handle the supplied
payload, `/add` returns `415 Unsupported Media Type`.

**`text`** — The literal text payload; valid **only** for
`type: "text"`. A non-text type (including `"html"`) is always routed
to the parser and must carry `uri` or `base64`; passing only `text` on
a non-text item returns `415`. To inline HTML as plain text, send it
as `type: "text"`.

**`uri`** — `http(s)://` or `file://` pointer to the asset. An
`http(s)` uri is fetched by the server and dispatched by the response
Content-Type (use it for assets hosted elsewhere — S3 / OSS presigned
URL, http server). A `file://` uri is read from the **server's** local
filesystem (the path must be reachable by the server process), subject
to these guardrails:

- the resolved path (symlinks followed) must be an existing regular file;
- its size must be ≤ `CORTI_MULTIMODAL__FILE_URI_MAX_BYTES` (default 50 MiB);
- when `CORTI_MULTIMODAL__FILE_URI_ALLOW_DIRS` is set, the path must lie
  within one of the listed roots (unset = any readable file, the
  permissive default).

A guardrail violation surfaces as `415`. Mutually exclusive with `base64`.

**`base64`** — Inline binary payload, base64-encoded (no `data:`
prefix). Mutually exclusive with `uri`.

> **Size caution.** base64 inflates the payload ~4/3× on the wire, and
> the encoded blob is held **verbatim in the server's staging buffer
> (SQLite) from `/add` until the session is flushed** — a multi-MB PDF
> becomes multi-MB of SQLite text for the buffer's lifetime. It is *not*
> persisted past extraction (the memory cell and episode store only the
> parsed text, never the raw bytes), but the transient footprint is
> real, and a large inline blob also slows request parsing. **Prefer
> `uri` (`http(s)://`) for large assets**: the buffer then holds only
> the URL plus the parsed text, and the bytes are fetched transiently at
> parse time rather than stored. Reserve `base64` for small assets or
> when no reachable URL exists.

**`ext`** — File-extension hint (`"pdf"`, `"png"`, `"html"`, ...). For
`base64` payloads this drives modality dispatch and is effectively
**required** (without it the server falls back to `mime`, then `415`s
if neither resolves). For `uri` payloads it is optional — the fetched
Content-Type usually suffices.

**`name`** — Filename / human label for logging and traceability.
Does not affect parsing.

**`extras`** — Free-form bag of provider-specific metadata (e.g.
caption, page hints). Opaque to Corti; passed through to the
multimodal LLM context.

### ToolCall

OpenAI-shaped tool invocation attached to an assistant turn.

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `id` | `string` | yes | — | Stable id; echoed by the paired `role: "tool"` turn via `tool_call_id` |
| `type` | `string` | no | `"function"` | Only `"function"` is meaningful today |
| `function` | [ToolFunction](#toolfunction) | yes | — | The function being called |

### ToolFunction

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | `string` | yes | Function name as registered with the agent |
| `arguments` | `string` | yes | **JSON-encoded string** (OpenAI convention — not an object) |

### FilterNode (filter DSL)

A recursive boolean tree of predicates. Used by `/search.filters` and
`/get.filters`. The Pydantic envelope only checks the recursive
combinator shape; field-level validity (which scalar fields are
filterable, which operators apply, value coercion) runs when the
node is compiled to a Postgres `where` clause server-side. Compile
errors surface as `422` with the offending field / operator in
`error.message`.

#### Shape

A node is a JSON object whose keys are one of:

| Key | Value | Notes |
|---|---|---|
| `AND` | `array<FilterNode>` | All child nodes must match. Omit if not needed |
| `OR` | `array<FilterNode>` | At least one child node must match. Omit if not needed |
| *<allowed field>* | scalar or operator map | Predicate on that field — see [Allowed fields](#allowed-fields) and [Operators](#operators) |

`AND`, `OR`, and scalar predicates **mix freely at the same level**;
they are implicitly joined with `AND`. A node with only scalar keys is
fine — `AND` / `OR` arrays are not required.

Example — `session_id == "demo-001"` AND (`sender_id` contains `"alice"` OR `"bob"`):

```json
{
  "session_id": "demo-001",
  "OR": [
    { "sender_id": "alice" },
    { "sender_id": "bob" }
  ]
}
```

<a id="filter-allowed-fields"></a>

#### Allowed fields

These are the only field names that may appear as a predicate key.
Anything else returns `422`.

| Field | Column kind | Value type | Notes |
|---|---|---|---|
| `session_id` | string | `string` | Session the row was extracted from |
| `parent_type` | string | `string` | Parent kind, e.g. `"memcell"` |
| `parent_id` | string | `string` | Parent id |
| `timestamp` | timestamp | `integer` (Unix epoch **ms** per v1 contract) or `string` (ISO-8601) | Implementation auto-detects `>= 10^12` as ms / smaller as sec for backward compat, but the contract is ms |
| `sender_id` | array of strings | `string` | Matches via `array_has(sender_ids, <value>)` — checks whether the row's `sender_ids` list contains the given id |

The following names are **reserved** and rejected inside `filters`.
They must be set at the top of the request (`/search` or `/get`),
not inside the DSL:

```
owner_id   owner_type   app_id   project_id
```

<a id="filter-operators"></a>

#### Operators

A predicate value may be a **scalar shorthand** (equality) or an
**operator map** (multiple operators in one dict are AND-joined):

| Operator | SQL | Applies to |
|---|---|---|
| `eq` | `=` | string, timestamp; on `sender_id` becomes `array_has(...)` |
| `ne` | `!=` | string, timestamp |
| `gt` | `>` | timestamp |
| `gte` | `>=` | timestamp |
| `lt` | `<` | timestamp |
| `lte` | `<=` | timestamp |
| `in` | `IN (...)` | string (list of values); on `sender_id` becomes `OR` of `array_has` |

Examples:

```json
// Equality shorthand
{ "session_id": "demo-001" }

// Operator map (operators inside one dict are AND-joined)
{ "timestamp": { "gte": 1748390400000, "lt": 1748400000000 } }

// IN list
{ "session_id": { "in": ["demo-001", "demo-002"] } }

// Array-membership on sender_ids
{ "sender_id": "alice" }                       // array_has(sender_ids, 'alice')
{ "sender_id": { "in": ["alice", "bob"] } }    // OR of two array_has
```

### SearchMethod

| Value | Behaviour |
|---|---|
| `"keyword"` | BM25 only — pure lexical match, no embedding cost |
| `"vector"` | Dense vector ANN only — semantic recall, no lexical |
| `"hybrid"` *(default)* | Reciprocal-rank fuse of BM25 + vector + optional scalar filter in a single Postgres query |
| `"agentic"` | Iterative cluster-path retrieval driven by a cross-encoder rerank loop; higher quality at higher latency / cost |

`"hybrid"` is the default because it balances recall and precision
with one Postgres roundtrip. `"agentic"` calls the LLM in a loop and
should be reserved for offline or background workflows.

### GetMemoryType

| Value | Track | Returned in `data.<plural>` |
|---|---|---|
| `"episode"` | user | `data.episodes` — [GetEpisodeItem](#getepisodeitem) |
| `"profile"` | user | `data.profiles` — [GetProfileItem](#getprofileitem) |
| `"agent_case"` | agent | `data.agent_cases` — [GetAgentCaseItem](#getagentcaseitem) |
| `"agent_skill"` | agent | `data.agent_skills` — [GetAgentSkillItem](#getagentskillitem) |

`memory_type` must match the requested owner kind: `"episode"` /
`"profile"` require `user_id`; `"agent_case"` / `"agent_skill"`
require `agent_id`. The mismatching combinations are rejected with
`422`.

## Endpoints

### POST /api/v1/memory/add

Append a batch of messages to a session buffer. The server durably
persists the raw messages and returns **immediately** — the LLM work
(boundary detection, extraction, markdown writes) runs asynchronously
on a per-session worker queue. See
[Eventual consistency](#eventual-consistency).

The response `status: "accepted"` means: raw content stored, extraction
queued. It does **not** report whether the boundary detector tripped —
that outcome is internal to the async pass.

#### Request body

| Field | Type | Required | Default | Constraints |
|---|---|---|---|---|
| `session_id` | `string` | yes | — | length 1–128 |
| `app_id` | `string` *(ScopeId)* | no | `"default"` | see [ScopeId](#scopeid-app_id-and-project_id) |
| `project_id` | `string` *(ScopeId)* | no | `"default"` | see [ScopeId](#scopeid-app_id-and-project_id) |
| `messages` | `array<MessageItem>` | yes | — | 1–500 items |

**`session_id`** — Identifies the conversation buffer on the server.
Messages POSTed with the same `(session_id, app_id, project_id)`
accumulate into one extraction batch. Different `session_id`s never
share a buffer, even within the same scope.

**`app_id` / `project_id`** — Scope identifiers; see
[ScopeId](#scopeid-app_id-and-project_id).

**`messages`** — Ordered list of [MessageItem](#messageitem) to
append. Order matters (it is preserved when the buffer is later
extracted). The 1–500 cap is a per-request safety bound, not a
session lifetime cap — you can call `/add` many times for the same
`session_id`.

#### Response body

`200 OK` returns a SuccessEnvelope wrapping:

| Field | Type | Notes |
|---|---|---|
| `message_count` | `integer` | Number of messages accepted in this call |
| `status` | `"accepted"` | Raw content durably stored; extraction queued |

**`message_count`** — Always equal to `len(messages)` on a `200` —
there is no partial accept. The field is present mainly for log
correlation.

**`status`** — Always `"accepted"` (async-accept contract): the messages
were written to the durable buffer and the boundary + extraction pass
is queued behind any earlier work for the same `session_id`. The old
synchronous outcomes (`"accumulated"` / `"extracted"`) no longer appear
in HTTP responses.

#### Failure semantics: durable-accept + async extraction (at-least-once)

`/add` is **not atomic**, and now it is not synchronous either:

1. **Accept (faithful, durable)** — the incoming messages are appended
   to the SQLite buffer (`INSERT OR IGNORE` keyed on the deterministic
   `message_id`, derived from `session_id + timestamp + index`). This
   write completes before the `200` is sent: a successful response
   means the raw content is already on disk.
2. **Extract (derived enhancement, async)** — boundary detection and
   the LLM-derived passes (atomic facts, foresight, user profile) run
   on the per-session worker queue, then the writers fan out to
   markdown + index rows.

If phase 2 fails, the phase-1 data is **already on disk** — the
endpoint is *at-least-once*: a failed extraction never loses messages.
Failures are logged server-side (`memorize_worker_run_failed`) and the
rows stay in the buffer; the next `/add` / `/flush` for the session (or
the boot-time recovery sweep) retries them.

**Blind retries are now safe.** Because `message_id` is deterministic,
re-POSTing the same payload (same timestamps) collides on the buffer PK
and is silently ignored — no duplicate rows, and the worker can never
carve the same message into two cells. Two caveats:

- A retry that **regenerates timestamps** (e.g. an LLM runtime that
  mints a new `timestamp` per attempt) produces new message ids and is
  *not* deduplicated — keep timestamps stable when retrying.
- `/add` still has no idempotency *key*; the dedup is derived from the
  payload itself.

**An ack is not a per-call episode.** `status: "accepted"` means the raw
content is durably buffered, nothing more. How that buffer is carved into
episodes is decided downstream by boundary detection, which reads the
conversation, not the calls: three `/add` calls inside one short window can
produce one episode (and two `/add` calls a second apart usually merge into
one), while a call whose content never reaches a boundary may not produce an
episode of its own at all. Measured on 2026-10-02: adds A+B+C in a 9-second
window produced one episode; adds D+E a second apart merged into one; every
one of them was nonetheless extracted into markdown and the index.

So a client must not treat "one ack per call" as "one memory per call", and
must not use the ack as proof that a later `/search` will return that
content — see [Eventual consistency](#eventual-consistency). To recall what
was written, search for it.

#### cURL example

```bash
TS=$(( $(date +%s) * 1000 ))
curl -X POST http://127.0.0.1:8000/api/v1/memory/add \
  -H 'Content-Type: application/json' \
  -d "{
    \"session_id\": \"demo-002\",
    \"app_id\": \"default\",
    \"project_id\": \"default\",
    \"messages\": [
      {\"sender_id\": \"alice\", \"role\": \"user\", \"timestamp\": $TS, \"content\": \"I love climbing in Yosemite every spring.\"},
      {\"sender_id\": \"alice\", \"role\": \"user\", \"timestamp\": $((TS+10)), \"content\": \"My favorite coffee shop is Blue Bottle in SOMA.\"},
      {\"sender_id\": \"alice\", \"role\": \"user\", \"timestamp\": $((TS+20)), \"content\": \"I bike to work most days.\"}
    ]
  }"
```

Response (real capture — returns in milliseconds; extraction runs in
the background afterwards):

```json
{
    "request_id": "ae78d3f689c941eea135893e702fd171",
    "data": {
        "message_count": 3,
        "status": "accepted"
    }
}
```

### POST /api/v1/memory/flush

Force the boundary detector to decide **now** for the given session
buffer. Like `/add`, this is async-accept: the flush work item is
queued **behind** any pending `/add` items for the same session (FIFO),
so it processes exactly what the adds left behind — typically a forced
extraction of the buffered tail. Useful at the end of a chat or agent
run to make sure pending context becomes durable memory.

#### Request body

| Field | Type | Required | Default | Constraints |
|---|---|---|---|---|
| `session_id` | `string` | yes | — | length 1–128 |
| `app_id` | `string` *(ScopeId)* | no | `"default"` | — |
| `project_id` | `string` *(ScopeId)* | no | `"default"` | — |

**`session_id`** — Identifies which buffer to flush. Must match the
`session_id` of prior `/add` calls in the same `(app_id, project_id)`
scope.

**`app_id` / `project_id`** — Scope identifiers; see
[ScopeId](#scopeid-app_id-and-project_id).

#### Response body

| Field | Type | Notes |
|---|---|---|
| `status` | `"accepted"` | Flush queued behind pending adds for the session |

**`status`** — Always `"accepted"`: the flush is queued, not done. The
old synchronous outcomes (`"extracted"` / `"no_extraction"`) no longer
appear in HTTP responses; use `/search` to observe the extraction
result once it lands.

#### cURL example

```bash
curl -X POST http://127.0.0.1:8000/api/v1/memory/flush \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"demo-002","app_id":"default","project_id":"default"}'
```

Response (real capture — instant, no LLM call on this path):

```json
{
    "request_id": "e65bcf4f56c042e39cdf50866810672c",
    "data": {
        "status": "accepted"
    }
}
```

### POST /api/v1/memory/search

Hybrid retrieval over the memory store. Combines BM25, dense vector
ANN, optional scalar filtering, optional cross-encoder rerank, and
optional final LLM rerank. Returns ranked items grouped by kind.

#### Request body

| Field | Type | Required | Default | Constraints |
|---|---|---|---|---|
| `user_id` | `string \| null` | XOR with `agent_id` | `null` | `minLength=1` if set |
| `agent_id` | `string \| null` | XOR with `user_id` | `null` | `minLength=1` if set |
| `app_id` | `string` *(ScopeId)* | no | `"default"` | — |
| `project_id` | `string` *(ScopeId)* | no | `"default"` | — |
| `query` | `string` | yes | — | `minLength=1` |
| `method` | [SearchMethod](#searchmethod) | no | `"hybrid"` | — |
| `top_k` | `integer` | no | `-1` | `-1` or `1..100` |
| `radius` | `number \| null` | no | `null` | `0.0 ≤ x ≤ 1.0` if set |
| `min_score` | `number \| null` | no | `null` | `0.0 ≤ x ≤ 1.0` if set |
| `include_profile` | `boolean` | no | `false` | — |
| `enable_llm_rerank` | `boolean` | no | `false` | — |
| `filters` | [FilterNode](#filternode-filter-dsl) `\| null` | no | `null` | — |

**`user_id` / `agent_id`** — **Exactly one** must be set. Determines
which track is searched: `user_id` → user-memory (episodes /
profiles); `agent_id` → agent-memory (cases / skills).

**`app_id` / `project_id`** — Scope identifiers; results never cross
scopes.

**`query`** — The retrieval query. The same string is fed to BM25
tokenization and to the embedding model, then the two recall lists
are fused.

**`method`** — Retrieval method; see [SearchMethod](#searchmethod).
Default `"hybrid"` is the recommended starting point.

**`top_k`** — Maximum number of items to return per kind. Two modes:
- `-1` (default) — "Use server defaults." Recall falls back to a
  fixed internal cap, and a server-side `radius` default kicks in if
  the caller did not pass one (see `radius` below). Use for "give me
  whatever you find worth returning."
- `1..100` — Explicit cap. Recall is sized as `top_k × multiplier`
  and **no** default `radius` is applied unless you set it yourself.

Values of `0` or outside `-1` / `1..100` are rejected with `422`.

**`radius`** — Optional **cosine-similarity threshold** in `[0.0, 1.0]`.
Candidates whose score is below this value are dropped — use it to cut
a long tail of weak matches. Three-level fallback for the effective
radius:

1. Caller-supplied `radius` (including the literal `0.0`) always wins.
2. With `top_k=-1` and no caller-supplied `radius`, a server-side
   default radius kicks in.
3. With `top_k>0` and no caller-supplied `radius`, no threshold is
   applied (`null`).

**`min_score`** — Optional **post-fusion relevance floor** in
`[0.0, 1.0]`. Results below this score are evicted after fusion,
independent of `radius` (which is a per-recall cosine threshold).

**`include_profile`** — When `user_id` is set, also fetch the user's
profile and include it in `data.profiles`. The profile is not
ranked; `score` is `null`. Ignored when `agent_id` is set.

**`enable_llm_rerank`** — Opt-in LLM rerank pass for
`method: "hybrid"`. Applies to `agent_case` and `agent_skill` fusion
only; the episode hybrid path has built-in fact eviction and
ignores this flag. Adds one LLM call per request. Ignored by
`keyword` / `vector` (no fusion to rerank) and `agentic` (uses its
own cross-encoder loop).

**`filters`** — Optional filter-DSL node; see
[FilterNode](#filternode-filter-dsl). Applied **before** ranking, so
it does not perturb the ranker.

#### Threshold guidance

- **Always pass `top_k` — a positive value (`1..100`) is recommended.**
  Production paths (the LLM-runtime plugins and daemon clients) always
  send `top_k > 0`; the server then trusts the truncation cap to drop
  the low-quality tail and applies **no** default `radius` (see the
  three-level fallback above).
- **`top_k=-1` ("unlimited") must be paired with an explicit
  threshold.** If you deliberately request unlimited recall, also pass
  `radius` and/or `min_score` (e.g. `0.4` or `0.5`). "Unlimited
  without a threshold" is logically contradictory: unless you set one,
  the server silently falls back to its internal `0.5` cosine default
  (`_DEFAULT_UNLIMITED_RADIUS`) — the effective quality floor is chosen
  by the server, not by you — and with no floor at all (explicit
  `radius: 0.0`) the response is dominated by low-quality candidates.
- **The `0.5` cosine lower bound only matters when hand-calling the
  bare API without `top_k`.** It is exactly the server-side default
  described above: `top_k=-1` with no caller-supplied `radius`, which
  is what a manual call omitting `top_k` (default `-1`) produces. Any
  call that passes `top_k > 0` is unaffected — no default radius is
  applied, and only an explicit `radius` / `min_score` acts as a
  threshold.

##### Measured score bands, and what a "no results" control actually needs

A nearest-neighbour search always has neighbours, so with `top_k > 0` and no
threshold an unrelated query returns the closest rows rather than nothing.
Measured on the live corpus (2026-10-02, `qwen3.7-text-embedding`, one
scope, `top_k=5`):

| Query | `keyword` | `vector` (cosine) | `hybrid` (fused) |
|---|---|---|---|
| a real paraphrase of stored work | 0 hits | 0.59–0.67 | 0.18–0.27 |
| `zzz qwerty flibbertigibbet` | **0 hits** | 0.46–0.48 | 0.10–0.13 |
| `capital of Peru altitude` | 1 weak hit | 0.34–0.89 | 0.06–0.67 |

Two consequences:

- **`keyword` is the only method with a clean negative.** BM25 scores zero
  when no term is shared, so it needs no threshold to prove "nothing
  matches". A test that wants a genuine empty result under `vector` or
  `hybrid` must pass `min_score` (≈ `0.2` separates the observed bands) — a
  zero-hit expectation without one is a mistake in the test, not a bug in
  the server.
- **Do not set a default floor for agent-facing recall.** The bands are
  close (a real paraphrase at 0.18 vs unrelated at 0.13 on `hybrid`) and the
  fused scale shifts again when a leg is degraded — a BM25-only fused result
  for a real match measured **0.008**. Any single default would drop real
  memories to suppress weak ones, and a missed memory is worse than a weak
  one the model can ignore. Prune with `top_k`; set `min_score` when you
  know the method's scale and the query's shape.

#### Response body

`200 OK` returns a SuccessEnvelope wrapping `SearchData`. All five
arrays are always present so client code can iterate without
branching on owner type; arrays that do not apply to the requested
owner kind stay as `[]`.

| Field | Type | Notes |
|---|---|---|
| `episodes` | `array<SearchEpisodeItem>` | Populated when `user_id` is set |
| `profiles` | `array<SearchProfileItem>` | Populated when `user_id` is set **and** `include_profile=true` |
| `agent_cases` | `array<SearchAgentCaseItem>` | Populated when `agent_id` is set |
| `agent_skills` | `array<SearchAgentSkillItem>` | Populated when `agent_id` is set |
| `unprocessed_messages` | `array<UnprocessedMessageDTO>` | Populated **only** when `filters.session_id` is a top-level eq scalar; otherwise stays `[]`. Independent of `user_id` / `agent_id` (buffer rows have no owner attribution — boundary detection runs before owner inference) |

#### SearchEpisodeItem

User-track conversation episode hit. `score` is the fused retrieval
score; `atomic_facts` lists single-sentence facts that matched the
query within this episode (already nested, no separate call needed).

| Field | Type | Notes |
|---|---|---|
| `id` | `string` | `<user_id>_ep_<YYYYMMDD>_<NNN>` |
| `user_id` | `string \| null` | Owner of this episode |
| `app_id` | `string` | Scope where the episode lives |
| `project_id` | `string` | Scope where the episode lives |
| `session_id` | `string` | The `session_id` whose messages produced this episode |
| `timestamp` | `string` | ISO-8601 with timezone offset; default `UTC` renders as `Z`, non-UTC defaults render as `±HH:MM` — see [Conventions](#conventions) |
| `sender_ids` | `array<string>` | Distinct `sender_id`s in the underlying messages |
| `summary` | `string` | Short summary (~200 chars), suitable for list-view rendering |
| `subject` | `string` | One-line subject / title |
| `episode` | `string` | Full extracted narrative |
| `type` | `"Conversation"` | Reserved; today only conversation-derived episodes ship |
| `score` | `number` | Fused retrieval score for this episode |
| `atomic_facts` | `array<SearchAtomicFactItem>` | Sub-facts extracted from the same episode that matched the query |

#### SearchAtomicFactItem

A single-sentence fact pulled out of an episode during extraction.

| Field | Type | Notes |
|---|---|---|
| `id` | `string` | `<user_id>_af_<YYYYMMDD>_<NNN>` |
| `content` | `string` | The fact as a single sentence |
| `score` | `number` | Same score scale as the parent episode |

#### SearchProfileItem

User profile. Only populated when `include_profile=true` and
`user_id` is set.

| Field | Type | Notes |
|---|---|---|
| `id` | `string` | Profile id |
| `user_id` | `string \| null` | Owner |
| `app_id` | `string` | Scope |
| `project_id` | `string` | Scope |
| `profile_data` | `object` | Free-form structured profile fields produced by extraction (schema is profile-specific) |
| `score` | `number \| null` | `null` for direct fetches (no query-aware profile ranking yet) |

#### SearchAgentCaseItem

Agent-track case hit. Returned only when the request uses `agent_id`.

| Field | Type | Notes |
|---|---|---|
| `id` | `string` | `<agent_id>_ac_<YYYYMMDD>_<NNN>` |
| `agent_id` | `string` | Owner |
| `app_id` | `string` | Scope |
| `project_id` | `string` | Scope |
| `session_id` | `string` | The agent run this case came from |
| `task_intent` | `string` | What the agent was trying to do (one-sentence framing) |
| `approach` | `string` | How the agent went about it |
| `quality_score` | `number` | Quality assessed at extraction time, in `[0, 1]` |
| `key_insight` | `string \| null` | One-line takeaway; `null` if extraction did not produce one |
| `timestamp` | `string` | ISO-8601 UTC of when the run happened |
| `score` | `number` | Fused retrieval score for this case |

#### SearchAgentSkillItem

Agent-track skill hit — a named procedural memory produced by
clustering related cases.

| Field | Type | Notes |
|---|---|---|
| `id` | `string` | `<agent_id>_sk_<YYYYMMDD>_<NNN>` |
| `agent_id` | `string` | Owner |
| `app_id` | `string` | Scope |
| `project_id` | `string` | Scope |
| `name` | `string` | Skill name (a stable, human-meaningful slug) |
| `description` | `string` | One-line description |
| `content` | `string` | Full skill body (procedure / playbook text) |
| `confidence` | `number` | Extraction confidence in `[0, 1]` |
| `maturity_score` | `number` | Maturity assessed at clustering time, in `[0, 1]` |
| `source_case_ids` | `array<string>` | `agent_case` ids that contributed to this skill |
| `score` | `number` | Fused retrieval score for this skill |

#### UnprocessedMessageDTO

A raw message still sitting in the boundary-detection buffer — sent to
`/add` but not yet carved into an episode / case. Returned **only**
when the request's `filters` contains `session_id` as a top-level eq
scalar (`{"session_id": "<sid>"}`); compound shapes (`AND` / `OR`
combinators, operator maps such as `{"eq": ...}` / `{"in": ...}`) do
not trigger the lookup because there is no defensible buffer-scope
mapping for them. Buffer rows have no `user_id` / `agent_id`
attribution, so `session_id` is the only meaningful query dimension.

| Field | Type | Notes |
|---|---|---|
| `id` | `string` | Original `message_id` from `/add` |
| `app_id` | `string` | Scope where the buffered message lives |
| `project_id` | `string` | Scope where the buffered message lives |
| `session_id` | `string` | Matches the requested `filters.session_id` |
| `sender_id` | `string` | Original sender id from `/add` |
| `sender_name` | `string \| null` | Original sender name; `null` if not provided |
| `role` | `"user" \| "assistant" \| "tool"` | Original role |
| `content` | `string \| array<object>` | `string` for the single-text shorthand, `array` of opaque content items for the original multimodal payload (mirrors [MessageItem.content](#messageitem)) |
| `timestamp` | `string` | ISO-8601 with timezone offset — see [Conventions](#conventions) |
| `tool_calls` | `array<object> \| null` | Original tool_calls payload if any |
| `tool_call_id` | `string \| null` | Original tool_call_id if any |

#### cURL example

```bash
curl -X POST http://127.0.0.1:8000/api/v1/memory/search \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "alice",
    "app_id": "default",
    "project_id": "default",
    "query": "Where do I like to climb?",
    "top_k": 5
  }'
```

Response (real capture):

```json
{
    "request_id": "3a8ad3dcd2484fe4a05ccbd89d438704",
    "data": {
        "episodes": [
            {
                "id": "alice_ep_20260528_00000001",
                "user_id": "alice",
                "app_id": "default",
                "project_id": "default",
                "session_id": "demo-002",
                "timestamp": "2026-05-28T11:30:36Z",
                "sender_ids": ["alice"],
                "summary": "On May 28, 2026 at 11:30 AM UTC, Alice shared that she loves climbing in Yosemite every spring...",
                "subject": "Alice's Outdoor Activities and Daily Routine on May 28, 2026: Climbing, Coffee, and Biking",
                "episode": "On May 28, 2026 at 11:30 AM UTC, Alice shared that she loves climbing in Yosemite every spring, highlighting a recurring seasonal activity. She also mentioned that her favorite coffee shop is Blue Bottle located in SOMA, indicating a preferred local spot. Additionally, Alice stated that she bikes to work most days, describing a habitual mode of transportation.",
                "type": "Conversation",
                "score": 0.6298899054527283,
                "atomic_facts": [
                    {
                        "id": "alice_af_20260528_00000001",
                        "content": "Alice said she loves climbing in Yosemite every spring.",
                        "score": 0.6298899054527283
                    }
                ]
            }
        ],
        "profiles": [],
        "agent_cases": [],
        "agent_skills": [],
        "unprocessed_messages": []
    }
}
```

### POST /api/v1/memory/get

Paginated listing of memory records of a given kind for a single
owner. No ranking — ordering is `sort_by` × `sort_order` only. Used
for UI browsing, exports, or filtered scans.

#### Request body

| Field | Type | Required | Default | Constraints |
|---|---|---|---|---|
| `user_id` | `string \| null` | XOR with `agent_id` | `null` | `minLength=1` if set |
| `agent_id` | `string \| null` | XOR with `user_id` | `null` | `minLength=1` if set |
| `app_id` | `string` *(ScopeId)* | no | `"default"` | — |
| `project_id` | `string` *(ScopeId)* | no | `"default"` | — |
| `memory_type` | [GetMemoryType](#getmemorytype) | yes | — | — |
| `page` | `integer` | no | `1` | `≥ 1` |
| `page_size` | `integer` | no | `20` | `1..100` |
| `sort_by` | `"timestamp" \| "updated_at"` | no | `"timestamp"` | — |
| `sort_order` | `"asc" \| "desc"` | no | `"desc"` | — |
| `filters` | [FilterNode](#filternode-filter-dsl) `\| null` | no | `null` | — |

**`user_id` / `agent_id`** — **Exactly one** must be set, and it must
match the track implied by `memory_type` (`"episode"` / `"profile"`
require `user_id`; `"agent_case"` / `"agent_skill"` require
`agent_id`).

**`app_id` / `project_id`** — Scope identifiers.

**`memory_type`** — Which item kind to list; see
[GetMemoryType](#getmemorytype). The route populates exactly one of
the four arrays in `data` based on this value.

**`page`** — 1-indexed page number. Together with `page_size`
determines the window. The response's `total_count` reports how many
items match the request before paging.

**`page_size`** — Items per page, in `1..100`. Default 20.

**`sort_by`** — Column to sort by. Note: for `memory_type` values
where `"timestamp"` does not apply (e.g. `"profile"` has no
timestamp, `"agent_skill"` is a named entity), the server silently
falls back to `"updated_at"`.

**`sort_order`** — `"desc"` (newest first, default) or `"asc"`.

**`filters`** — Optional [FilterNode](#filternode-filter-dsl) for
predicate-based filtering before pagination.

#### Response body

`200 OK` returns a SuccessEnvelope wrapping `GetData`. The four
arrays are always present so client code can iterate without
branching on `memory_type`; exactly one is populated.

| Field | Type | Notes |
|---|---|---|
| `episodes` | `array<GetEpisodeItem>` | Populated when `memory_type="episode"` |
| `profiles` | `array<GetProfileItem>` | Populated when `memory_type="profile"` |
| `agent_cases` | `array<GetAgentCaseItem>` | Populated when `memory_type="agent_case"` |
| `agent_skills` | `array<GetAgentSkillItem>` | Populated when `memory_type="agent_skill"` |
| `total_count` | `integer` | Total matching records **before** paging |
| `count` | `integer` | Number of items in **this page** (`len(items)`) |

#### GetEpisodeItem

Same shape as [SearchEpisodeItem](#searchepisodeitem) **minus**
`score` and `atomic_facts` (listing is unranked and does not nest
sub-facts).

| Field | Type | Notes |
|---|---|---|
| `id` | `string` | `<user_id>_ep_<YYYYMMDD>_<NNN>` |
| `user_id` | `string \| null` | Owner |
| `app_id` | `string` | Scope |
| `project_id` | `string` | Scope |
| `session_id` | `string` | Originating session |
| `timestamp` | `string` | ISO-8601 with timezone offset, identical to `/search` (both go through the same `from_iso_format` path) — see [Conventions](#conventions) |
| `sender_ids` | `array<string>` | Distinct `sender_id`s in the underlying messages |
| `summary` | `string` | Short summary |
| `subject` | `string` | One-line subject |
| `episode` | `string` | Full extracted narrative |
| `type` | `"Conversation"` | — |

#### GetProfileItem

| Field | Type | Notes |
|---|---|---|
| `id` | `string` | Profile id |
| `user_id` | `string \| null` | Owner |
| `app_id` | `string` | Scope |
| `project_id` | `string` | Scope |
| `profile_data` | `object` | Free-form structured profile fields |

#### GetAgentCaseItem

Same shape as [SearchAgentCaseItem](#searchagentcaseitem) **minus**
`score`.

| Field | Type | Notes |
|---|---|---|
| `id` | `string` | `<agent_id>_ac_<YYYYMMDD>_<NNN>` |
| `agent_id` | `string` | Owner |
| `app_id` | `string` | Scope |
| `project_id` | `string` | Scope |
| `session_id` | `string` | Originating run |
| `task_intent` | `string` | What the agent was trying to do |
| `approach` | `string` | How it went about it |
| `quality_score` | `number` | Quality assessed at extraction time |
| `key_insight` | `string \| null` | One-line takeaway, if any |
| `timestamp` | `string` | ISO-8601 UTC |

#### GetAgentSkillItem

Same shape as [SearchAgentSkillItem](#searchagentskillitem) **minus**
`score`.

| Field | Type | Notes |
|---|---|---|
| `id` | `string` | `<agent_id>_sk_<YYYYMMDD>_<NNN>` |
| `agent_id` | `string` | Owner |
| `app_id` | `string` | Scope |
| `project_id` | `string` | Scope |
| `name` | `string` | Skill name |
| `description` | `string` | One-line description |
| `content` | `string` | Full skill body |
| `confidence` | `number` | Extraction confidence in `[0, 1]` |
| `maturity_score` | `number` | Maturity assessed at clustering, in `[0, 1]` |
| `source_case_ids` | `array<string>` | `agent_case` ids that contributed to this skill |

#### cURL example

```bash
curl -X POST http://127.0.0.1:8000/api/v1/memory/get \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "alice",
    "app_id": "default",
    "project_id": "default",
    "memory_type": "episode"
  }'
```

Response (real capture):

```json
{
    "request_id": "b8ba1255440147a684885f2084f6a25d",
    "data": {
        "episodes": [
            {
                "id": "alice_ep_20260528_00000001",
                "user_id": "alice",
                "app_id": "default",
                "project_id": "default",
                "session_id": "demo-002",
                "timestamp": "2026-05-28T11:30:36Z",
                "sender_ids": ["alice"],
                "summary": "On May 28, 2026 at 11:30 AM UTC, Alice shared that she loves climbing in Yosemite every spring...",
                "subject": "Alice's Outdoor Activities and Daily Routine on May 28, 2026: Climbing, Coffee, and Biking",
                "episode": "On May 28, 2026 at 11:30 AM UTC, Alice shared that she loves climbing in Yosemite every spring, highlighting a recurring seasonal activity. She also mentioned that her favorite coffee shop is Blue Bottle located in SOMA, indicating a preferred local spot. Additionally, Alice stated that she bikes to work most days, describing a habitual mode of transportation.",
                "type": "Conversation"
            }
        ],
        "profiles": [],
        "agent_cases": [],
        "agent_skills": [],
        "total_count": 1,
        "count": 1
    }
}
```
### Runtime interop endpoints

These three endpoints exist for **agent-runtime plugins**, not for tool
callers. Each returns *finished text* — the exact string a runtime injects
into its model's context — so recall thresholds, block formats, recency
sampling and degradation reporting live in one place instead of being
re-implemented, and drifting, inside every plugin.

An adapter's whole job is to move these strings between its host's hook
protocol and the wire. Rationale and migration history:
[adr/0004-runtime-interop-endpoints.md](adr/0004-runtime-interop-endpoints.md).

All three share the request scope `user_id` (required), `app_id`,
`project_id`, `agent_id`, `session_id` — and, like every other 200 in this
API, they wrap their payload in the standard `{request_id, data}` envelope.
The field tables below describe `data`.

#### POST /api/v1/memory/session/start

Once per session. Returns breadth — owner profile, the previous session's
summary, and a catalog of recent episodes — as one injectable block.

The catalog is **sampled**: by default 10 entries are drawn at random from
the 200 most recent memory records, so one long session cannot occupy every
slot. The block says so out loud, because a random fragment must not be
mistaken for a summary of a finished task. Set `recency_sample: 0` for a
plain newest-first list of `recent_count` entries instead.

| Field | Type | Default | Notes |
|---|---|---|---|
| `recency_window` | `int` | 200 | Newest records the sample is drawn from |
| `recency_sample` | `int` | 10 | Catalog size; `0` switches to newest-first |
| `recent_count` | `int` | 5 | Used only when `recency_sample` is `0` |
| `max_chars` | `int` | 4000 | Ceiling for the whole block |
| `include_profile` | `bool` | `true` | Include the owner-profile line |

Response: `block` (inject verbatim), `display` (one human line), `catalog`,
`total_episodes`, `last_session`, `profile_line`.

#### POST /api/v1/memory/prefetch

Once per user turn. Returns the recall block for one prompt.

| Field | Type | Default | Notes |
|---|---|---|---|
| `query` | `string` | — | Required |
| `method` | `SearchMethod` | `hybrid` | Same values as `/search` |
| `top_k` | `int` | 5 | Hits injected |
| `min_score` | `float` | `0.0` | Floor on the injected hits; `0.0` disables it |
| `max_chars` | `int` | 3500 | Ceiling for the block |
| `include_profile` | `bool` | `true` | Prepend the owner-profile line |

`skipped` is the interesting field. It is `"trivial_prompt"` when the
prompt carries no retrievable intent (`"ok"`, `"thanks"`, a host slash
command, anything under four characters) and `"no_relevant_hits"` when
recall came back empty. **Neither is an error**: the adapter injects
nothing and lets the turn proceed. Only a `null` `skipped` comes with a
non-empty `block`.

On a healthy provider `min_score: 0.0` is right. Recall scores are not
comparable across methods — the same episode measured 0.0079 fused on the
HYBRID path and 0.2078 on the lexical path — so a single shared floor
drops every hybrid hit and looks exactly like an empty memory. Prune with
`top_k`; set a floor only when you know your method's scale.

#### POST /api/v1/memory/session/end

Once per finished session. Records the digest that a later
`session/start` reports as "Last session".

The adapter parses its own transcript — transcript shapes are
host-specific — and sends the result. Where the digest is *stored* is
server state, which is why the summary is visible to every runtime rather
than only to the one that wrote it.

| Field | Type | Notes |
|---|---|---|
| `session_id` | `string` | Required |
| `first_prompt` | `string` | Drives the one-line summary |
| `turn_count` | `int` | Reported in the display line |
| `started_at` / `ended_at` | `datetime` | ISO-8601 or epoch; drives the duration |
| `reason` | `string` | e.g. `logout`, `clear` |

The write is idempotent on `(app_id, project_id, user_id, session_id)`: a
repeated `session/end` refreshes the existing row instead of appending a
duplicate.

#### Degradation reporting

`/search`, `/prefetch` and `/session/start` all report `degraded:
string[]` **inside `data`**, beside the results it qualifies. It is empty on
a healthy provider; `["embedding"]` means recall ran on the keyword leg
alone and `["rerank"]` means the first-stage order was kept.

When a leg is missing, `prefetch` also prepends a one-line notice to `block`
so the model knows the ranking it received is partial. The request still
succeeds — degradation is reported, never fatal.

Do not drop this field on the floor. "Nothing relevant was found" and "the
semantic leg is down, so this was a lexical answer" are different facts, and
a client that renders them identically leaves the model unable to tell an
empty store from an impaired one.


### POST /api/v1/ome/trigger

Manually trigger a registered OME strategy.

#### Request body

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | Strategy name (e.g. `reflect_episodes`) |
| `timeout` | `float` | no | `120.0` | Max seconds to wait for completion |
| `force` | `bool` | no | `false` | Bypass the `enabled` gate in `ome.toml` |

#### Response body

`200 OK` returns:

| Field | Type | Notes |
|---|---|---|
| `status` | `"ok" \| "timeout"` | Whether the strategy completed within the timeout |
| `name` | `string` | Echoes the requested strategy name |

#### Errors

- `404` — strategy name not found in the OME registry.

#### cURL example

```bash
curl -X POST http://127.0.0.1:8000/api/v1/ome/trigger \
  -H 'Content-Type: application/json' \
  -d '{"name": "reflect_episodes", "force": true}'
```

---

### Knowledge endpoints

The knowledge base subsystem (`/api/v1/knowledge/*`) provides document
upload, CRUD, and hybrid search. These endpoints are fully documented
in their own reference: **[docs/knowledge.md](knowledge.md)**.

Summary of available routes:

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/v1/knowledge/documents` | Upload and extract a document |
| `GET` | `/api/v1/knowledge/documents` | List documents (paginated) |
| `GET` | `/api/v1/knowledge/documents/{doc_id}` | Get a single document |
| `PUT` | `/api/v1/knowledge/documents/{doc_id}` | Replace a document |
| `PATCH` | `/api/v1/knowledge/documents/{doc_id}` | Partial update |
| `DELETE` | `/api/v1/knowledge/documents/{doc_id}` | Delete a document |
| `GET` | `/api/v1/knowledge/topics/{topic_id}` | Get a single topic |
| `POST` | `/api/v1/knowledge/search` | Hybrid search over topics |
| `GET` | `/api/v1/knowledge/categories` | List taxonomy categories |

---

## OpenAPI spec source

This document mirrors the OpenAPI 3.x spec that FastAPI auto-generates
from the Pydantic DTOs. **The spec is not exposed by default** — the
`/openapi.json`, `/docs` (Swagger UI), and `/redoc` endpoints only
mount when the server starts with `ENV=DEV` set.

```bash
ENV=DEV corti server start
# In another terminal:
curl http://127.0.0.1:8000/openapi.json | python -m json.tool
# Or interactively in a browser:
open http://127.0.0.1:8000/docs
```

This document and the spec are kept in sync by hand. Where they
disagree, the **spec is the structural ground truth** (field names,
required flags, value constraints). This document carries the
**business semantics** the spec cannot — when to use which method,
the eventual-consistency caveat, why a field is shaped the way it is.

---

For higher-level context (cascade design, DDD layering, on-disk
layout), see [architecture.md](architecture.md) and
[storage_layout.md](storage_layout.md).
