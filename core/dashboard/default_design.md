# DESIGN.md — Editorial Data Desk (dashboard default)

The shipped default design language for workspace dashboards. Copy this to
`workspaces/<your-workspace>/DESIGN.md` and edit to give that workspace its own
look — the dashboard engine reads the token block below; no code change needed.

## 1. Visual Theme & Atmosphere
A financial-broadsheet "data desk": warm paper canvas, characterful serif
masthead, mono datelines, one sharp accent, hairline rules. Credible for serious
data — restrained, editorial, not flashy.

## 2. Color Palette & Roles
- `paper`  #f3efe6  — page canvas (warm cream)
- `card`   #fbf9f3  — panel/card surface
- `ink`    #1b1a17  — primary text
- `ink_soft` #6f6a60 — secondary/meta text
- `rule`   #d7d1c4  — primary hairline rule
- `rule_soft` #e7e2d6 — soft inner rule
- `accent` #b4441c  — single-series + headline figures (sienna)
- `accent_deep` #2f4452 — secondary accent (slate)

Multi-series charts use a colorblind-safe categorical ramp (legibility wins over
brand purity per the dashboard quality contract):
- `categorical`: #0072b2, #d55e00, #009e73, #cc79a7, #e69f00, #56b4e9, #f0e442, #000000

## 3. Typography Rules
- `serif`: 'Fraunces', Georgia, serif      — display/masthead/headline figures
- `sans`:  'Hanken Grotesk', system-ui, sans-serif — body
- `mono`:  'Spline Sans Mono', ui-monospace, monospace — datelines, labels, figures

## 7. Do's and Don'ts
- DO keep one dominant accent; let data color come from the categorical ramp.
- DO keep charts contained, legends present, percent axes 0-100.
- DON'T sacrifice series separability to brand color — the verify gate enforces
  perceptual distance and contrast.

## 8. Design References (idea galleries — for layout/color inspiration only)
External galleries to study for executive-grade layout, semantic color, hero-KPI
pill rows, and one-chart-per-question density. Reference ideas only; the shipped
look stays the Editorial Data Desk tokens below.
- Power BI dashboard & report examples (hundreds; finance/healthcare/KPI/HR/CRM):
  https://zoomcharts.com/en/microsoft-power-bi-custom-visuals/dashboard-and-report-examples/
- Tableau RCM / healthcare starter kit (revenue-cycle layout):
  https://www.tableau.com/blog/starter-kit-II-revenue-cycle-management-dashboard
- Patterns observed worth adopting: hero KPI "pill" row (4-6, not 10); semantic
  green/amber/red on deltas & vs-target; one lead chart + detail table per page;
  distinct per-category chart color (bad = red); generous whitespace.

```design-tokens
paper: #f3efe6
card: #fbf9f3
ink: #1b1a17
ink_soft: #6f6a60
rule: #d7d1c4
rule_soft: #e7e2d6
accent: #b4441c
accent_deep: #2f4452
serif: 'Fraunces', Georgia, serif
sans: 'Hanken Grotesk', system-ui, sans-serif
mono: 'Spline Sans Mono', ui-monospace, monospace
categorical: #0072b2, #d55e00, #009e73, #cc79a7, #e69f00, #56b4e9, #f0e442, #000000
```
