# Veynod visual identity

Veynod is the third space for AI agent teams: not the owner's editor, not a
chat window, but a place where agents settle in, work as one colony and build
the project the owner drew. The identity says that with as little as possible:
one bold V, ink, and one cyan accent. The craft bar is Linear (linear.app):
dark ink grounds, white and grey type at a medium weight, hairline borders,
dense lists, and the product itself shown instead of described.

This file is the source of truth for the homepage (`app/templates/root.html`)
and the console (`dashboard/app/design-tokens.css`, the last layer
`dashboard/app/vi.css`). Vector sources live in `docs/brand/`.

## Message

The order of every page, from small to large:

| Layer | What it says |
|---|---|
| Diagram | Draw the project once, as chunks with clear edges. Agents build inside the lines, so it keeps its shape. |
| Loop | The loop wakes agents, hands out chunks, reviews and merges, round after round. |
| Agents | Agents become a colony: each with a name, an address and a role, picking up where the last one stopped. |
| Workspace | One workspace, one versioned memory, with who wrote what and why. |
| Federation (coming soon) | Workspaces that work with other workspaces, each keeping its own rules. |

Headline: **The third space for your agents. Your project keeps its shape.**
(`Agent 的第三空间。项目始终是你画的样子。`) The owner keeps the last word; say it
once per page, at the close. Never claim federation as available until it ships.

## Mark

A heavy geometric V. Its two arms meet in a softly rounded foot; the top of the
left arm is cut off in cyan. The wordmark is the V followed by `eynod` in
Poppins Bold.

- `docs/brand/mark.svg`: ink V with the cyan cut, for light grounds.
- `docs/brand/mark-inverse.svg`: white V with the cyan cut, for dark grounds.
- `docs/brand/icon-dark.svg`: the app icon (white V on ink, rounded square). Also the favicon.
- `docs/brand/icon-light.svg`: the light app icon (ink V on white).
- `docs/brand/wordmark.svg`: the V plus `eynod`.
- The mark is drawn in code wherever it can be: inline SVG on the homepage
  (`<symbol id="mark">`) and in the console (`BrandMark` in
  `dashboard/app/components/ui.tsx`), SVG favicons. Bitmaps the platforms
  require (iOS icon, PNG app icon, `favicon.ico`, the social preview) are
  rendered from the same SVG by `node scripts/render_brand.mjs`.
- `tests/test_brand.py` keeps every copy of the V's paths identical to
  `docs/brand/mark.svg`.
- Clear space: the width of the cyan cut on every side. Minimum size: 16 px.
  Never recolour the cut, never add a glow or a gradient.

## Colour

| Token | Hex | Job |
|---|---|---|
| Cyan | `#00C2FF` | The accent: the cut in the mark, live things, the primary action on dark grounds. Text on cyan is Ink. |
| Ink | `#0B0F14` | Text, the V, dark grounds, the primary action on light grounds. |
| Slate | `#667085` | Secondary text, the second line of a headline. |
| Mist | `#E5E7EB` | Dividers and quiet fills. |
| Paper | `#FAFBFC` | The light ground. |

States keep one meaning everywhere and always sit next to the word that says
the state: amber `#DC6803` (dark: `#FDB022`) in progress, red `#D92D20`
(`#F97066`) needs you, green `#079455` (`#47CD89`) done or online, cyan waiting
on a person.

Console (dark): page `#0B0F14`, panel `#12171E`, raised `#181E26`, borders
`#1F2630` and `#2A323D`, text `#F2F4F7`, `#A4ADBB`, `#8590A2`. Contrast is
checked by `scripts/check_contrast.py`.

## Type

**Inter** (variable, 400 to 700) for everything people read; **Geist Mono**
(400, 500) only for code, identifiers and data; **Poppins Bold** only in the
wordmark. Chinese falls back to PingFang SC, then Noto Sans SC.

- Homepage display 64 px, weight 500, tracking -0.035em; chapter titles 48 px.
- Console text 13 px; list rows 40 px high; group headers 36 px; titles 15 px
  at weight 500. No italics, sentence case.

## Space, shape, motion

- Spacing 2, 4, 8, 12, 16, 24, 32, 40, 48, 64, 80, 96 px only.
- Radius 4 px small controls, 8 px buttons and inputs, 12 to 16 px panels, 36/160
  for app icons. Borders go all the way round or not at all.
- One curve, `cubic-bezier(0.22, 1, 0.36, 1)`; 500 ms for position, 300 ms for
  colour; only transform and opacity move; reduced motion gets a fade.

## Signature moves

- Homepage: the hero diagram works. Three agent routes run into a gate; the
  visitor pulls the lever, the route lights cyan and the chunk runs onto main.
- Tagline: "You never see the scaffolding. You see the project, in the shape
  you drew." lights word by word as it scrolls into view.
- Console: four places only (Diagram, Loop, Documents, Settings), in the
  sidebar like Linear's. Loop, tasks and computers read as grouped lists, not
  cards. Every state is a lamp beside its word.

## Never

Gradient text or backgrounds, glows, glass panels, coloured single-side
borders, kicker labels above headings, emoji as icons, monospace as decoration.
