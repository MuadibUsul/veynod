# services/

Backend processes. One modular monolith plus the processes that must scale or fail on their own
(ARCH-001 §0, ADR-001).

| Directory        | What                                                              | Created by                 |
| ---------------- | ----------------------------------------------------------------- | -------------------------- |
| `api/`           | Modular business API: identity, consent, profile, dates, payments | `ser-p01-03`, `ser-p01-05` |
| `voice-gateway/` | Realtime voice session control behind `VoiceProvider`             | `ser-p02-01`, `ser-p02-02` |
| `worker/`        | Matching, negotiation, booking and refund jobs                    | `ser-p03-05`, `ser-p04-06` |

Models and agents never hold database, payment-provider or merchant credentials; they call domain
services that check consent first (ARCH-001 §3).
