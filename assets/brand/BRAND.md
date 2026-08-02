# Attest brand assets

The mark is a triangular constellation that reads as an "A": an apex node, two base
nodes, a crossbar edge, and a sealed centre point. The apex is the claim, the base is the
evidence it rests on, and the crossbar is what closes the structure — verification is the
edge that turns a floating assertion into a rigid shape.

The wordmark is Cormorant Garamond regular, tracked +0.02em, converted to outlines. No
webfont is required for any file here.

## Files

| File | Use |
|---|---|
| `attest-mark.svg` | Primary mark. `currentColor` — set `color` in CSS. |
| `attest-mark-small.svg` | Below ~32px. Heavier stroke, crossbar joints and centre ring dropped. |
| `attest-wordmark.svg` | Wordmark alone. Use when the mark is already present in the same viewport. |
| `attest-lockup-horizontal.svg` | Default lockup. Nav bars, document headers, README. |
| `attest-lockup-stacked.svg` | Centred contexts: splash, login, print. |
| `favicon.svg` + `favicon-32.png` / `favicon-16.png` | Browser tab. |
| `apple-touch-icon.svg` / `.png` | iOS home screen, 180px. |
| `attest-social-card.svg` / `.png` | 1200×630 OG image. Devpost header, link previews. |
| `attest-lockup-{light,dark}-1200.png` | Raster lockups for slide decks and Devpost body. |
| `attest-mark-{light,dark}-512.png` | Raster mark, transparent background. |

## Colour

| Token | Hex | Use |
|---|---|---|
| Ink | `#14141A` | Mark and wordmark on light surfaces |
| Bone | `#F5F3EE` | Mark and wordmark on dark surfaces |
| Void | `#08080B` | Product background, social card field |
| Icon field | `#0E0E12` | Favicon and app icon background |

The mark is monochrome by design. It never carries a verdict colour — the verdict colours
belong to the claim rows, not to the identity. Do not tint the mark green for Supported or
red for Contradicted.

## Clearspace and minimum size

Clearspace on all sides equals the height of the apex node circle (10% of mark height).
Nothing crosses it, including the particle field.

- Mark alone: 20px minimum. Below 32px use `attest-mark-small.svg`.
- Horizontal lockup: 120px wide minimum.
- Stacked lockup: 80px wide minimum.

## HTML

```html
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="icon" href="/favicon-32.png" sizes="32x32">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta property="og:image" content="https://your-domain/attest-social-card.png">
<meta name="twitter:card" content="summary_large_image">
```

Inline in React, so the mark inherits text colour and animates with it:

```tsx
import Mark from "./assets/attest-mark.svg?react";

<Mark className="h-6 w-auto text-bone/90 transition-colors hover:text-bone" />
```

## Placement notes

The landing hero already carries the serif wordmark at display size against the particle
field. Adding the mark there puts thin geometry on top of thin geometry — leave the hero as
it is. The mark earns its place where the background is flat: the nav bar, the favicon, the
Evidence & Benchmark page header, the published-claim artifact footer, and the Devpost
header image.

## Regenerating

Built with `fontTools` from `@fontsource/cormorant-garamond` v5.3.0. Geometry lives in
normalised 100 × 96 space: apex `(50, 10)`, bases `(10, 86)` and `(90, 86)`, crossbar at
`y = 62` spanning `x = 22.63` to `77.37`. Stroke weight is 2.6 units, which is 2.7% of mark
height — hold that ratio at any size.
