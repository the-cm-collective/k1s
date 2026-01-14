# Dashboard-Derived Style Guide

The dashboard UI defines the official k1s look. Use these tokens when styling docs pages and any new surfaces so the docs match `/dashboard`.

## Palette
- **Primary:** `#2563eb` (actions, focus) with soft variant `#3b82f6` and highlight `#60a5fa`.
- **Info:** `#0284c7` on `#e0f2fe` for neutral notices.
- **Success:** `#16a34a` on `#16a34a33`.
- **Warning:** `#f59e0b` on `#f59e0b33`.
- **Danger:** `#ef4444` on `#ef444433`.
- **Neutrals:** base `#0f172a` (page), surface `#111827`, panel `#1f2937`, border `#334155`/`#8884`, text `#e5e7eb`, muted text `#9ca3af`.
- **Links:** dark `#5a86c9` (hover `#7aa0e8`); light `#2f59b9` (hover `#3b63c5`).
- **Utility accents:** selection glow `#60a5fa`, selected state `#1f2937`, table lines `#8884`.

## CSS Tokens (drop into docs)
```css
:root {
  color-scheme: light dark;
  --k1s-bg: #0f172a;
  --k1s-surface: #111827;
  --k1s-panel: #1f2937;
  --k1s-border: #334155;
  --k1s-border-soft: #8884;
  --k1s-text: #e5e7eb;
  --k1s-text-muted: #9ca3af;
  --k1s-primary: #2563eb;
  --k1s-primary-soft: #3b82f6;
  --k1s-highlight: #60a5fa;
  --k1s-info: #0284c7;
  --k1s-info-bg: #e0f2fe;
  --k1s-success: #16a34a;
  --k1s-success-bg: #16a34a33;
  --k1s-warn: #f59e0b;
  --k1s-warn-bg: #f59e0b33;
  --k1s-danger: #ef4444;
  --k1s-danger-bg: #ef444433;
  --k1s-card-bg: #0001;
  --k1s-header-bg: #0a0a0a10;
  --k1s-radius: 8px;
  --k1s-radius-pill: 999px;
  --k1s-gap: 12px;
  --link: #5a86c9;
  --link-hover: #7aa0e8;
}
```

## Typography
- Font stack: `system-ui, -apple-system, "Segoe UI", "Roboto", sans-serif`.
- Sizes: 18px page title, 14px section titles, 13px table/body.
- Line height: 1.3 for dense data blocks; 1.5 for prose.

## Surfaces and Components
- Cards: 1px solid `var(--k1s-border-soft)`, radius 8px, background `var(--k1s-card-bg)`, padding 8–10px.
- Pills/badges: radius 999px, semantic fill from success/warn/danger tokens with matching text color.
- Buttons/inputs/selects: 1px border `var(--k1s-border-soft)`, radius 6px–8px, background `var(--k1s-card-bg)`, inherit text color; hover darkens to `#111827`.
- Tables: full-width, collapsed borders, row padding 6px, divider `var(--k1s-border-soft)`.
- Sticky header: translucent `var(--k1s-header-bg)` with slight blur to mirror the dashboard bar.

## Layout
- Grid bias: 210px fixed sidebar + fluid content; collapse to a thin rail on small screens.
- Spacing rhythm: 6px (tight), 8px (controls), 12px (gaps), 14px (section offsets).
- Scroll areas hide scrollbars but must remain keyboard-scrollable.

## How to apply to docs
1) Add the CSS tokens above to the docs CSS (or a shared `:root` block).  
2) Swap existing colors to the tokens; keep success/warn/danger semantics identical to the dashboard.  
3) Use the card/table/button rules for callouts, code boxes, and nav bars to keep structure aligned with `/dashboard`.  
4) When adding new components, default to the neutral surfaces and introduce only the primary blue or semantic colors for emphasis.  

The dashboard remains the source of truth—update these tokens if the dashboard palette changes.
