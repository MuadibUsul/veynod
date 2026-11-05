# infra/

| Directory     | What                                                     | Created by   |
| ------------- | -------------------------------------------------------- | ------------ |
| `docker/`     | Compose files and images for dev / staging / prod        | `ser-p01-02` |
| `migrations/` | PostgreSQL + PostGIS + pgvector migrations (up and down) | `ser-p01-03` |
| `monitoring/` | OpenTelemetry, dashboards, alerts                        | `ser-p01-07` |
| `terraform/`  | Only if managed infrastructure needs it later            | —            |

Environments never share a database or secrets (PLAN-001 P01-02).
