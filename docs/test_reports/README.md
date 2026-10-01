# Test reports

Iteration reports for the Corti memory plugin and its live DSH integration.
Each run is a separate file so successive dev/test iterations stay comparable.

**Naming**: `YYYY-MM-DD-<short-commit>-<slug>.md` — ISO date first, then the
repository commit the run was taken at, then a short topic slug.

| Date | Commit | Report | Outcome |
|---|---|---|---|
| 2026-10-02 | `8ba157c` | [Fix verification run (iteration 3)](2026-10-02-8ba157c-corti-memory-plugin-fix-verification.md) | F3/F4/F5 fixes verified live (hyphenated recall 0→2 hits, `degraded` on `data`, DSH surfaces it); **new**: Hermes bundle stale (pre-refactor), embedding leg still `degraded`, Hermes does not surface degradation textually |
| 2026-10-02 | `832d49e` | [Live acceptance run (iteration 2)](2026-10-02-832d49e-corti-memory-plugin-live-test.md) | `memory_flush` session-id fallback fixed; embedding leg still degraded (keyword-only recall); hyphenated identifiers not recallable; backend crash-loop during the run |

Iteration 1 lives in the "Live runtime verification" section of
[deepseek-harness-integration.md](../deepseek-harness-integration.md).
