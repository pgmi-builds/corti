# Corti Documentation

Documentation for [Corti](../README.md) — md-first memory extraction
framework. Organised by [Diátaxis](https://diataxis.fr/) — what kind of
question you have determines which section to read.

## Tutorials

Learning-oriented entry points — start here to get a feel for the system
before wiring it into a real workflow.

| Doc | Purpose |
|---|---|
| [hermes-integration.md](hermes-integration.md) | Wire Hermes Agent to Corti as a long-term memory backend — install, config, lifecycle, tools |
| [deepseek-harness-integration.md](deepseek-harness-integration.md) | Wire DeepSeek Harness to Corti (`corti-memory` plugin) — hooks, live runtime verification report, config |
| [corti-memory-plugin-verification.md](corti-memory-plugin-verification.md) | Live re-verification of the `corti-memory` DSH plugin — harness results, defects, measured write-path latency |
## Reference

Technical reference: contracts, commands, schemas — read these when you
already know what you want to do and need to know exactly how.

| Doc | Purpose |
|---|---|
| [api.md](api.md) | HTTP API v1 reference — endpoints, request / response, error contracts |
| [knowledge.md](knowledge.md) | Knowledge base module — upload, search, taxonomy, storage layout |
| [reflection.md](reflection.md) | Reflection — offline memory consolidation: enable, schedule, storage, triggering |
| [cli.md](cli.md) | `corti` CLI subcommands + env var conventions |
| [storage_layout.md](storage_layout.md) | Memory-root tree + frontmatter chassis + EntryId encoding |
| [prompt_slots.md](prompt_slots.md) | PromptSlot loader — bundled default prompts (Layer 1 live; app/runtime overlays planned) |
| [configuration.md](configuration.md) | TOML / env-var configuration reference |
| [multimodal.md](multimodal.md) | Multimodal content items — image / PDF / audio / doc parsing |

## Explanation

Design decisions and architectural concepts — read these to understand
why the system is shaped the way it is.

| Doc | Purpose |
|---|---|
| [overview.md](overview.md) | Project vision, scope, design philosophy |
| [how-memory-works.md](how-memory-works.md) | Storage stack + on-disk paths + write→index→read pipeline + consistency |
| [architecture.md](architecture.md) | DDD layered architecture + dependency rules |
| [datetime.md](datetime.md) | Two-zone discipline — UTC at storage, display tz at boundaries |

## How-to

Task-driven operational guides — read these when you need to do a
specific thing (drain a queue, recover from a stuck row, etc.).

| Doc | Purpose |
|---|---|
| [cascade_runbook.md](cascade_runbook.md) | Cascade subsystem ops — drain queue, recover stuck rows |

## Engineering / Internal

For maintainers and contributors working on the framework itself,
not for using it.

| Doc | Purpose |
|---|---|
| [engineering.md](engineering.md) | Engineering & dev-efficiency infrastructure (CI / tooling / Claude Code) |

## See also

Top-level project files live next to the repo root:

- [README.md](../README.md) — quick start & feature overview
- [CONTRIBUTING.md](../legal/CONTRIBUTING.md) — how to contribute (issue-only model)
- [ACKNOWLEDGMENTS.md](../legal/ACKNOWLEDGMENTS.md) — third-party acknowledgments

Coding conventions and slash command workflows are auto-loaded by
Claude Code from [.claude/rules/](../.claude/rules/) and
[.claude/skills/](../.claude/skills/).
