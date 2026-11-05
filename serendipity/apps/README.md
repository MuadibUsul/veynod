# apps/

User-facing applications. Each one is its own npm workspace (`apps/<name>/package.json`).

| Directory       | What                                                   | Created by                 |
| --------------- | ------------------------------------------------------ | -------------------------- |
| `mobile/`       | React Native + Expo app: voice, profile, dates, IM     | `ser-p01-06`               |
| `merchant-web/` | Venue confirmation and check-in (staff only)           | `ser-p04-03`, `ser-p04-08` |
| `ops-web/`      | Support, payments disputes, safety review (staff only) | `ser-p04-10`, `ser-p05-05` |

Apps never hold business rules: they call `services/api` and re-read server state after every push
(ARCH-001 §11.3).
