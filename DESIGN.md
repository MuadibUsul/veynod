# Veynod visual identity: Interlocking

Veynod's look comes from the railway signal box. Many routes (agents) are drawn
on one track diagram, signals (gates) guard every junction, and a person at the
lever frame pulls the last lever. The product works the same way: many agents
work on one board, quality gates guard each step, and the owner keeps the last
word. Everything visual follows from that one picture.

This file is the source of truth for the homepage (`app/templates/root.html`)
and the console (`dashboard/app/design-tokens.css` and the component CSS).
Assets live in `docs/brand/`.

## The world in one line per surface

| Surface | Scene | Ground |
|---|---|---|
| Homepage (Persuade) | A signal box by day: pale enamel panel, black track diagram, lamps lit where a route is set. | Light enamel |
| Console (Operate) | The control room on the night shift: dark room, lit routes, the colours of the lever frame on the shell. | Dark |

Both grounds are the same world. Light or dark is decided by the scene, never by
habit: a first visit happens in daylight on any screen; the console is open for
hours while agents work, often in the evening.

## Mark

A V drawn as two tracks joined at the foot. The right-hand route stops short
and its start is a lit amber signal lamp: one route is set, the line is clear,
a person pulled the lever. The lamp is the same lamp every state in the product
uses.

- `docs/brand/mark.svg`: the mark in ink, for light grounds.
- `docs/brand/mark-night.svg`: the mark for dark grounds.
- `docs/brand/mark-on-green.svg`: the app icon (enamel tracks on box green, amber lamp).
- `docs/brand/wordmark.svg`: mark plus `veynod`, set in Manrope Bold, lower case.
- Clear space: one lamp diameter on every side. Minimum size: 16 px (favicon).
- The lamp is always amber in the mark. Never recolour the tracks with a state
  colour, never add a glow, never place the mark on a photo. Never hang the
  lamp under the V's foot: that reads as a medal, not a signal.

## Colour

Colour comes from the lever frame and the lamps. Each colour has one job, and
the job is the same on every surface.

| Role | Day | Night | Job |
|---|---|---|---|
| Enamel | `#E7EBE5` | — | Homepage ground |
| Plate | `#F5F6F2` | `#181818` | Raised surfaces, panels |
| Ink | `#14201A` | `#ECEFE9` | Text, track lines |
| Ink 2 | `#3D4A43` | `#A9B3AC` | Secondary text |
| Ink 3 | `#56635B` | `#8E9890` | Muted text, captions |
| Rule | `#C6CEC5` | `#2A2E2B` | Dividers, track bed |
| Box green | `#123B2C` | `#10362A` | The signal box itself: console rail, homepage footer and closing band |
| Amber (route set) | `#F2A900` | `#F5B21B` | The one primary action; work in progress; the lamp in the mark. Text on amber is Ink. |
| Red (stop) | `#B8261A` | `#F06A5A` | Blocked, failed, needs you |
| Green (clear) | `#1E7A47` | `#4CC27E` | Passed, merged, done, online |
| Blue (locked) | `#1F4E9E` | `#86A9F0` | Waiting on a person: review, approval, a proposal |

Night ground steps: page `#131209`, subtle `#131209`, panel `#181818`, raised
`#1F1F1F`, borders `#272727` and `#313131`.

Rules:
- Amber marks the single primary action on a screen. A second action is a
  plain button.
- State colours appear as a lamp (a filled circle) beside a word, never as the
  only signal: the word always says the state.
- No gradients on backgrounds or text. No glows. Depth is a soft, offset shadow.

## Type

One family: **Manrope** (400, 500, 600, 700) for everything people read.
**Geist Mono** (400, 500) only for code, identifiers, numbers in tables and
data. Chinese falls back to PingFang SC, then Noto Sans SC, with a looser line
height (1.75) for running text.

- Display: 60 to 72 px, weight 700, tracking -0.03em, balanced lines.
- Headings: 30 / 24 / 20 px, weight 700 or 600.
- Body: 16 px on the homepage, 14 px in the console; line height 1.6.
- No italics. No weight above 700. Sentence case for headings and buttons.
- Sizes snap to the Tailwind scale (12, 14, 16, 18, 20, 24, 30, 36, 48, 60, 72).

## Space, shape, motion

- Spacing: 2, 4, 8, 12, 16, 24, 32, 40, 48, 64, 80, 96 px only.
- Radius: 4 px controls inside panels, 8 px buttons and inputs, 12 px panels;
  lamps and avatars are round. Nested shapes use outer radius minus the gap.
- Borders go all the way round or not at all.
- Motion: one curve, `cubic-bezier(0.22, 1, 0.36, 1)`; 500 ms for position,
  300 ms for colour. Only transform and opacity move. Reduced motion gets a
  plain fade.

## Signature moves

- **Homepage:** the hero is a working interlocking diagram. Three agent routes
  run into a signal; the visitor pulls the lever, the signal clears, the route
  lights amber and the chunk runs onto the main line, which turns green. This
  is the page's one authored motion.
- **Console:** every state is a lamp. Task, chunk, host and presence states use
  the same lamp and the same five colours, so a glance at the Loop page reads
  like a glance at a track diagram.
- **Tagline reveal:** "Many agents. One source of truth." lights word by word
  as it scrolls into view, like lamps along a route.

## Icons

Phosphor (regular weight, 1.5 px stroke at 24 px), drawn as inline SVG. No
emoji, no Unicode glyphs as icons.

## Never

Gradient text, neon glows, glass panels, coloured single-side borders, kicker
labels above headings, section numbers, cards of icon plus heading plus text as
the page structure, monospace as decoration.
