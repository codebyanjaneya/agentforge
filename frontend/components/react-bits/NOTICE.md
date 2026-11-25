# Third-party code in this directory

The components in this directory are copied from **React Bits** (https://reactbits.dev, https://github.com/DavidHDev/react-bits) by David Haz, installed with the official
shadcn registry (`@react-bits`, free components only; React Bits Pro is not used).

They are licensed under **MIT + Commons Clause** (see `LICENSE.md`, copied unchanged from the upstream repository). The licence allows using them as part of an application,
website or product; it does not allow selling the components themselves. The copyright and licence notice must stay with them: do not delete `LICENSE.md` or this file.

**No React Bits component is vendored here at present.** The registry is configured (`components.json`, entry `@react-bits`, install with
`npx shadcn@latest add @react-bits/<Name>-TS-TW`) so one can be added deliberately. The licence and this notice stay in place for that reason: if a component is added, its
use and any local change to it are recorded in the table below, and the licence file must stay next to it.

## Evaluated for P0 and not adopted (2026-11)

Each was installed alone, previewed in a throw-away route in both themes, with reduced motion and with forced colors, and measured (gzipped JavaScript the browser actually
transfers, from resource timing, against a plain control page). The code and the `motion` dependency were then removed.

| Component | Cost | Findings | Decision |
|---|---|---|---|
| `ShinyText` | +2.9 KB | Real text is exposed to assistive technology; reduced motion honoured; readable in forced-colors. Its colours are parsed through a canvas, so CSS theme variables do not work and literal colours chosen for the dark theme have poor contrast on the light theme (an ember sweep falls below AA); it drives a requestAnimationFrame loop for a decorative effect on a sentence that already shows a spinner and the real elapsed seconds; the vendored file fails this repository's `react-hooks/refs` lint rule. | Not adopted: no material improvement, contrast risk |
| `BlurText` | +41.0 KB (needs `motion`) | Over the whole +40 KB budget for a one-time headline effect; renders a `<p>` (invalid inside a heading); fails `react-hooks/refs` and `react-hooks/set-state-in-effect` lint three times. | Not adopted |

## Vendored components in use

| Component | Used in | Local changes |
|---|---|---|
| (none) | | |
