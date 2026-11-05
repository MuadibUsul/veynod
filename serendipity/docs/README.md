# docs/

The product documents are kept in the Veynod workspace, namespace `serendipity`, and are the source
of truth; do not copy them here.

| Document                | Veynod address                    |
| ----------------------- | --------------------------------- |
| PRD — 完整产品设计      | `doc://product-prd`               |
| ARCH-001 — 技术架构设计 | `doc://technical-architecture`    |
| PLAN-001 — 工程实施方案 | `doc://engineering-delivery-plan` |

Work is tracked as the Veynod task tree `serendipity-mvp` (EPIC-00 … EPIC-06, task keys `ser-pNN-NN`).

This directory holds what belongs next to the code:

- `adr/` — architecture decision records for this codebase ([ADR-0001](adr/0001-monorepo-location-and-toolchain.md)).
- `runbooks/` — incident runbooks from PLAN-001 §12.2 (`ser-p05-05`).
- `compliance/` — engineering notes for the privacy data ledger (`ser-p00-07`).
