# image-to-dxf

A [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) **skill** that turns a drawing — a photo, a screenshot, a scan — into a DXF, and files every run into a local material library.

中文说明见下方 [中文](#中文).

---

## The idea in one paragraph

Vectorising a drawing tells you **where the ink is**. It does not tell you **where the part is**, and it cannot read a single number off the page. A drawing's real content is `Ø23`, `R60`, `53`, `128°` — and those numbers determine the geometry exactly. So this skill does the opposite of the obvious thing: it looks at the picture first, copies the dimensions down, and **computes** the entities (tangency, concentricity, patterns) instead of fitting them to pixels. Tracing is kept for the parts the numbers do not pin down.

Measured on one connecting link, same drawing:

| | traced | reconstructed from dimensions |
|---|---|---|
| entities | 35 fragments | **12** |
| scale | 1 unit = 1 pixel (meaningless) | **real mm** |
| profile | broken, dimension lines mixed in | **closed** |
| tangent arcs | fitted approximation | **exact** |

The link's outline — two bosses `Ø36/Ø17` and `Ø15/Ø8`, centres `53` apart, joined by `R80` and `R160` arcs — is solved, not guessed. See [`examples/link_03.spec.json`](examples/link_03.spec.json).

## Install

The repo root **is** the skill folder, so cloning it into a skills directory is the whole install:

```bash
git clone https://github.com/guludaren/image-to-dxf.git ~/.dsh/skills/image-to-dxf
# or into a single project:  <project>/.dsh/skills/image-to-dxf
```

Then give the scripts a Python that has the tracer:

```bash
pip install autocad-mcp          # the tracer this skill drives
# or use a checkout's venv, e.g. /path/to/autocad-mcp/.venv/Scripts/python.exe
```

`spec_builder.py` and `cad_library.py` need only `ezdxf` (+ `opencv-python` for image fingerprints). `trace_image.py` additionally needs `autocad_mcp`.

## What's in here

| File | What it does |
|---|---|
| `SKILL.md` | The workflow the agent follows: look → copy dimensions → rebuild → trace the gaps |
| `scripts/spec_builder.py` | Builds a DXF from a dimension spec. `tangent_arc` solves the arc tangent to two circles |
| `scripts/trace_image.py` | Runs the raster tracer **twice** (Otsu and adaptive) and writes a comparison |
| `scripts/cad_library.py` | The material library: archive, similarity search, parameter reuse |
| `examples/link_03.spec.json` | The verified link — 12 entities, closed profile |
| `docs/measurements.md` | The raw numbers behind every claim below |

## Two things this skill refuses to do

Both are backed by measurements in [`docs/measurements.md`](docs/measurements.md).

**1. It does not guess the binarisation threshold.** Across ten drawings, Otsu claimed 15–39% of the page as ink on nine of them — which looks like "it is eating the background". On the dark screenshots it was nevertheless the *better* choice (33 entities vs adaptive's 465). The "high ink share" rule and the "contours per entity" rule each get one of the two known cases wrong, with a margin as thin as 5.1 vs 5.7. So `trace_image.py` runs both, writes both previews, and hands the choice to a pair of eyes.

**2. The library does not lend parameters across source classes.** A paper photo's `adaptive` recipe applied to a dark screenshot took the entity count from 33 to 461. A recipe is only offered when the nearest record is close *and* came from the same kind of source. **The library advises; the measurement decides.**

## Known limits

- **Text is not read by the tracer.** The `Ø23` on the page does not exist in a traced DXF. That is the whole reason for the reconstruction path — the numbers come from looking at the picture.
- **Dimension lines, arrows and leaders are vectorised too**, mixed in with the outline.
- **`adaptive` explodes on noisy images**: one example went from 169 to 2 666 contours, 33 to 465 entities, 6 s to 2 minutes.
- **Photos of screens** carry moiré, glare and a bright border that the binariser counts as ink.
- **Isometric / 3D views must not be traced**: a circle is an ellipse there, so 2D fitting reads it wrong.
- **Non-circular curves** (splines, ellipses, involutes) fit only approximately; the tracer reports the residual.
- `fillet` (rounding between two lines) is declared but not implemented — give the arc its centre and radius instead.

## 中文

把零件图/工程图（照片、截图、扫描件）变成 DXF 的 DeepSeek Harness 技能，并且**每次处理都归档进本地素材库**。

**核心思路**：矢量化只能告诉你"墨在哪"，它**读不出图上的数字**。可图纸的真正内容是 `Ø23`、`R60`、`53`、`128°`——这些数把几何完全定死了。所以本技能反过来做：**先看图把尺寸抄下来，再用几何约束（相切、同轴、阵列）把实体算出来**；只有数字没定下来的部分才交给矢量化。

同一个连杆：矢量化 35 个碎片、比例是"1 单位 = 1 像素"、轮廓断裂还混着尺寸线；按尺寸重建 **12 个实体、真实 mm、闭合轮廓、相切弧数学精确**。

**安装**：仓库根目录**就是**技能目录，clone 到技能路径即可：

```bash
git clone https://github.com/guludaren/image-to-dxf.git ~/.dsh/skills/image-to-dxf
```

**两条不做的原则**（都有实测数据支撑，见 `docs/measurements.md`）：

1. **不猜二值化阈值**。十张图里九张的 Otsu 墨占比在 15%~39%，看着像"把背景吃进去了"，但深色截图上恰恰是 Otsu 更好（33 实体 vs adaptive 465）。两条启发式各会判错一个已知用例，margin 薄到 5.1 vs 5.7。所以两版都跑，交给人看预览定。
2. **素材库不跨来源类别借参数**。把纸面照片的 adaptive 配方套到深色截图上，实体数从 33 炸到 461。只有"最近邻足够近**且**来源类别相同"才给配方。**库只建议，实测说了算。**

## License

MIT — see [LICENSE](LICENSE). The tracer it drives, [autocad-mcp](https://github.com/puran-water/autocad-mcp), is a separate MIT project and is **not** vendored here.
