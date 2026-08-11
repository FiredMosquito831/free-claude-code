# Brand — My Claude Code (MCC)

This document is the **single source of truth** for the My Claude Code brand.
When copy, docs, the dashboard, or release assets conflict with anything here,
this file wins.

> Rebrand context: the product was "Free Claude Code" (FCC). The package was
> renamed `free_claude_code` → `my_claude_code` and the product rebranded to
> **My Claude Code**. A dual command family is preserved (`mcc-*` primary,
> `fcc-*` legacy aliases) so existing installs and muscle memory keep working.

## 1. Name & abbreviation

- **Name:** My Claude Code
- **Abbreviation:** MCC
- **Package:** `my-claude-code` (PyPI / wheel)
- **Server command:** `mcc-server` (legacy alias: `fcc-server`)
- **Launchers:** `mcc-claude`, `mcc-codex`, `mcc-pi`, `mcc-claude-old`
  (legacy aliases: `fcc-claude`, `fcc-codex`, `fcc-pi`, `fcc-claude-old`)

## 2. Positioning

A **local control plane** for the models, keys, routes, and coding agents the
user chooses. MCC is an Anthropic-compatible local proxy that sits between a
coding agent (Claude Code, Codex, Pi, and their IDE extensions) and whichever
model provider the user configures — forwarding requests, rotating keys, and
translating responses back into Anthropic's wire format on the user's own
machine.

## 3. Voice

Precise, local-first, active, factual, and explicit about trade-offs.

- State what the software does; do not overpromise.
- **Never promise "free"** as a product claim, and **never imply an Anthropic
  affiliation**. Anthropic is a third-party model provider like any other; MCC
  is independent.
- Prefer concrete mechanics ("rotates keys on a 429") over vague benefits.
- Name trade-offs when they exist (e.g. deferred Windows install, offline
  catalog limits).
- Use the active voice and imperative for instructions (`Run mcc-server`).

## 4. Primary line

> **Your models. Your keys. Your machine.**

Use this as the hero/positioning line. It is the canonical summary of the
product's local-first stance.

## 5. Signature mark — the "MC route glyph"

The brand mark is a compact **MC route glyph**: two routing lanes converging
through a single control point (the local proxy). It visualises the product's
core mechanic — many models/agents in, one controlled route out.

Rules:

- **Use the route glyph**, not gradient initials, emoji, or a generic AI
  sparkle. The glyph is the only approved logo mark.
- Render it monochrome or in the palette accent; do not add gradients, glow, or
  photographic elements.
- Keep it small and geometric. It reads at 16px favicon scale and at dashboard
  header scale.

## 6. Typography

Local/system-based fonts for **instant offline load** — no web-font fetch.

- **Fira Code** (monospace) for: routes, model references, commands, tokens,
  code, and tabular data.
- **Fira Sans** (sans) for body copy and UI labels.

Fall back to the system UI monospace / sans stack if Fira is unavailable; never
block render on a font download.

## 7. UI palette

Three themes, selected via a `[data-theme]` attribute on the dashboard root.
All colors are **semantic tokens** built on primitive tokens (see
`src/my_claude_code/api/admin_static/admin.css`).

| Theme | Purpose | Base |
| --- | --- | --- |
| **Midnight** | Dark operations console (default) | Deep navy/black surfaces, high-contrast text |
| **Paper** | True light | White/near-white surfaces, dark text |
| **High Contrast** | WCAG AAA | Maximum contrast pairings for both modes |

Token tiers: **primitives → semantic → component**. Components reference
semantic tokens only; never hardcode raw hex in component styles. Charts read
colors through the `token()` helper so they re-theme automatically.

## 8. Motion

Subtle only. 300–400ms, `power1.out` easing. Respect
`prefers-reduced-motion`: disable non-essential animation when set.

## 9. Accessibility (non-negotiable)

- 44px minimum touch targets.
- 4.5:1 text contrast (AAA in High Contrast theme).
- Visible focus rings on every interactive element.
- ARIA labels on icon-only controls; charts ship companion data tables.
- No emoji as icons.

## 10. Preserved legacy contracts (do NOT rebrand)

These published contracts stay exactly as they are, for backward
compatibility, even though the product is now My Claude Code:

- `FCC_*` environment variables (e.g. `FCC_ENV_FILE`, `FCC_OPEN_BROWSER`,
  `FCC_SMOKE_TARGETS`).
- Release repository `FiredMosquito831/free-claude-code` (RELEASE_REPO).
- Local proxy port `:8082`.
- Proxy auth token `freecc`.
- Model ids `claude-3-freecc-*`.
- Codex provider id `fcc`.
- Pi scope `free-claude-code/**`.
- Config directory `.fcc`.
- Legacy command names `fcc-*` (kept as aliases).
- Display-name constant `LEGACY_DISPLAY_NAME = "Free Claude Code"`.

When writing docs or UI, prefer the new `mcc-*` names but note that the `fcc-*`
aliases still work.
