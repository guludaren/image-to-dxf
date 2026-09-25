# Measurements behind this skill's rules

Every claim in the README and SKILL.md comes from these runs. Recorded so the
rules can be re-checked rather than taken on faith.

Hardware: one Windows machine, `autocad-mcp` from a local checkout (OpenCV
5.0.0, ezdxf 1.4.4), ten example drawings pasted as phone photos of screens and
paper.

## 1. Ink share per threshold, ten drawings

`binarize()` with each threshold, reported as the share of pixels it calls ink.

| image | otsu % | adaptive % |
|---|---|---|
| 01_hook_paper | 31.62 | 2.61 |
| 02_draft_2d_light | 25.29 | 11.32 |
| 03_tangent_dark | 15.92 | 8.07 |
| 04_iso_dark_a | 23.48 | 14.86 |
| 05_iso_dark_b | 3.89 | 7.38 |
| 06_draft_2d_b | 20.50 | 12.39 |
| 07_flange_center | 38.86 | 15.86 |
| 08_two_views | 17.87 | 12.24 |
| 09_hook_dark | 25.06 | 12.64 |
| 10_iso_dark_c | 34.77 | 16.57 |

A "high ink share means Otsu is eating the background" rule looks obvious here.
Section 2 shows why it is wrong.

## 2. The two cases that break every simple rule

| image | threshold | entities | dangling | blobs | contours | fit_rejected | fragments |
|---|---|---|---|---|---|---|---|
| 01_hook_paper | otsu | 17 | 32 | 103 | 224 | 52 | 73 |
| 01_hook_paper | **adaptive** | **87** | 149 | 48 | 121 | 66 | 66 |
| 09_hook_dark | **otsu** | **33** | 52 | 35 | 169 | 78 | 64 |
| 09_hook_dark | adaptive | 465 | 849 | 141 | 2666 | 710 | 355 |

- **Otsu ink share picks wrong on 09.** It reads 25.06%, over any sane ceiling,
  yet Otsu is the better trace (33 entities vs 465). The photo includes the
  bright room around the monitor, which becomes a large ink region that the
  blob filter then discards — the drawing survives.
- **Contours per entity picks right on both, but barely.** 01: 224/17 = 13.2
  (otsu) vs 121/87 = 1.4 (adaptive) — a clear call. 09: 169/33 = 5.1 (otsu) vs
  2666/465 = 5.7 (adaptive) — a 12% margin, not something to build a rule on.
- **Entity count alone picks wrong on 09** (465 > 33).
- **Dangling ratio does not discriminate at all**: 1.88, 1.71, 1.58, 1.83.

Conclusion encoded in `trace_image.py`: run both, show both previews, let the
eyes decide; keep a provisional pick only so unattended runs produce something.

## 3. Cost of the wrong threshold

`adaptive` on the noisy 02 and 06 examples took 2+ minutes each and produced
3 152 and 2 364 entities, against 118 and 107 for Otsu. A comparison sweep over
ten images with both thresholds did not finish inside a 10-minute budget.

## 4. The library leak that changed the design

First version let the library override the measured threshold with the nearest
neighbour's recipe. Tracing `09_hook_dark` after filing `01_hook_paper`
(paper photo, `adaptive`) took entities from **33 to 461** and fragments from 64
to 355 — a worse drawing, produced confidently.

Fix: a recipe is offered only when the nearest record is within distance 0.4
**and** its `source_kind` matches. The library now returns
`"binarisation recipe would not transfer"` for that pair, and the run keeps its
measured default.

## 5. Python compile cache (unrelated, measured while here)

`NODE_COMPILE_CACHE` on this machine's dsh web boot: 16.53 s cold, 14.73 s and
14.62 s warm. A ~1.5 s saving against a ~15 s floor dominated by plugin-tree
loading — not the lever it looks like.
