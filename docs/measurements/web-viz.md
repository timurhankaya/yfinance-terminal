# The terminal's SVG primitives

What the drawing layer costs the browser. Two decisions rest on it: that
a treemap is drawn as SVG rather than canvas, and that it is capped at
400 cells (`docs/superpowers/specs/2026-09-08-web-terminal-faz2c-viz-design.md`,
"Kararlar" 10). Neither was to be defended by argument.

Measured with vitest 4.1's benchmark runner, Node 22.15, jsdom, on Apple
silicon (macOS 26.5, arm64):

```
npm --prefix web exec -- vitest bench --run src/panels/viz/treemap.bench.ts
npm --prefix web exec -- vitest bench --run src/panels/viz/sparkline.bench.ts
```

The render half goes through `renderToStaticMarkup`, not a jsdom mount.
The question is what it costs to BUILD four hundred cells' worth of
elements; a jsdom commit would have measured jsdom.

## Treemap — 2026-09-08

`squarify` alone, over a market-shaped distribution (a few large values
and a long tail):

| Cells | mean | p75 | p99 | ops/s |
|---|---|---|---|---|
| 50 | 0.0067 ms | 0.0065 ms | 0.0092 ms | 148,800 |
| 200 | 0.0333 ms | 0.0328 ms | 0.1207 ms | 30,100 |
| 400 | 0.0780 ms | 0.0734 ms | 0.1607 ms | 12,800 |

Layout and the React elements together:

| Cells | mean | p75 | p99 | ops/s |
|---|---|---|---|---|
| 50 | 0.55 ms | 0.54 ms | 0.80 ms | 1,830 |
| 200 | 1.35 ms | 1.29 ms | 2.85 ms | 740 |
| 400 | 2.39 ms | 2.47 ms | 2.88 ms | 418 |

Cutting a 5,000-item list down to 400 (sort plus the "Other" box):
0.093 ms mean.

**Result: SVG holds, and the layout is not what costs.** A 400-cell
treemap is 2.4 ms to build, of which the squarified layout is 0.078 ms —
**3 %**. The rest is React building ~1,600 elements, which is the term
canvas would remove; at 2.4 ms against a 16.7 ms frame it is not worth
removing. Scaling is close to linear in both halves (50 → 400 cells is
8x the cells for 11.6x the layout and 4.4x the render), so there is no
superlinear term hiding above the cap.

**Why the cap is 400 and not "as many as there are".** Not this timing:
2.4 ms would allow several thousand. It is the picture. Over a 960x540
box, 400 cells average 1,300 px² — a 36x36 square — and the label
threshold in `Treemap.tsx` is 46x18, so most of them already carry no
text. Past that a treemap stops being readable before it becomes slow,
which is why the tail becomes one named box rather than a thousand
unlabelled ones.

**Canvas stays unbuilt** until a measurement contradicts this one.

## Sparkline at watchlist size — 2026-09-08

`WLA` draws up to 200 rows, each with a month of closes.

| Page | mean | p75 | p99 | ops/s |
|---|---|---|---|---|
| 20 rows x 30 points | 0.219 ms | 0.212 ms | 0.430 ms | 4,560 |
| 100 rows x 30 points | 1.12 ms | 1.12 ms | 1.47 ms | 892 |
| 200 rows x 30 points | 2.35 ms | 2.22 ms | 5.32 ms | 426 |

One cell, by window length:

| Points | mean | ops/s |
|---|---|---|
| 5 | 0.0066 ms | 150,500 |
| 30 | 0.0125 ms | 80,200 |
| 90 | 0.0227 ms | 44,100 |

**Result: a full watchlist of sparklines is one 2.4 ms mount.** That is
paid once, when the list loads — not per tick. The window length is
cheap enough that the 90-point ceiling costs 0.023 ms a cell, so the
default of 30 is a choice about what a reader can see rather than about
what the browser can afford.

**The re-render cost is zero, and that is asserted rather than timed.**
A tick re-renders its row (`live/hooks.test.tsx`), and the sparkline in
that row is memoised over an array that did not change, so it is not
redrawn: `viz/Sparkline.test.tsx` asserts the memo, and
`panels/spark.test.tsx` asserts that the same DOM node survives a tick.
A timing would have measured the harness.

**What is not measured.** The request. `/ui/api/sparklines` reads up to
200 symbols x 90 sessions in one statement over the `price_history`
primary key; the route's own caps are what bound it, and the query has
no measurement of its own yet.
