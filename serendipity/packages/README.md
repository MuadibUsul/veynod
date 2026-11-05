# packages/

Shared libraries, consumed as TypeScript source by the apps and services.

| Directory       | What                                                       | Created by                           |
| --------------- | ---------------------------------------------------------- | ------------------------------------ |
| `domain/`       | State machines and invariants; no I/O                      | seeded by `ser-p01-01`; `ser-p04-01` |
| `consent/`      | Principal, purpose, scope; `authorize()` denies by default | `ser-p01-05`                         |
| `schemas/`      | Zod / JSON schemas and API types                           | first task that needs them           |
| `ai-adapters/`  | Voice, vision and LLM provider adapters                    | `ser-p02-01`, `ser-p02-07`           |
| `a2a-protocol/` | `a2a-match/0.1` messages and the deterministic verifier    | `ser-p03-04`, `ser-p03-07`           |
| `ui/`           | Design tokens and shared components                        | `ser-p01-06`                         |

`domain/`, `consent/` and the payment modules need owner review (see `CONTRIBUTING.md`).
