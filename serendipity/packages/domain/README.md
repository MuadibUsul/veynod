# @serendipity/domain

The deterministic kernel: state machines and invariants, with no I/O, no clock and no randomness
passed in implicitly. Payment, booking and relationship rules are decided here and only here; models
propose, this package decides (ARCH-001 ADR-003).

Today it holds two helpers the state machines will use:

- `assertNever(value)` — the `default` of every `switch` over a union, so a new state or event fails
  to compile until every switch handles it.
- `Brand<T, Name>` — nominal identifiers, so a `UserId` can never be passed where a `DateId` is
  expected.

The date state machine (ARCH-001 §8.1) lands here with `ser-p04-01`. Changes to this package need
owner review (see `../../CONTRIBUTING.md`).
