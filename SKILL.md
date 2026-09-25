---
name: image-to-dxf
description: 把一张零件图/工程图（照片、截图、扫描件）变成 DXF，并归档进本地素材库。两条路：先从图上的尺寸数字重建精确几何，再用矢量化补数字没定的部分。触发词：画成CAD、出DXF、图纸转DXF、image to DXF、把这个零件画出来、矢量化这张图、trace drawing、图纸入库、素材库、CAD重建。
whenToUse: 用户给了一张图纸/零件图/工程图照片或截图，想要 DXF、CAD 文件、矢量图；或者要查询、复用本地素材库里已有的图纸。
---

# image-to-dxf

把图纸变成 DXF。**默认走"按尺寸重建"，矢量化只作为补充和交叉校验。**

## 第 0 步：先看图（这一步不能跳）

用 `read_image` 打开图，然后回答四个问题：

1. **这是什么？** 零件名/类型（吊钩、连杆、法兰、支板、轴…）。
2. **什么视图？** 单视图 / 多视图 / 等轴测（3D 立体）。
3. **有哪些尺寸数字？** 把 Ø / R / 角度 / 长度**逐个抄下来**，这些是重建的原料。
4. **什么来源？** 纸面照片 / 屏幕截图 / 导出渲染 / 扫描件。

第 3 问是全部价值所在。矢量化的结果里**没有数字**——它只知道像素在哪。数字必须由看图得到。

**等轴测图直接放弃矢量化**：轴测里圆是椭圆，2D 拟合会把圆弧读歪。要么让用户提供正投影视图，要么只按标注重建。

## 第 1 步：按尺寸重建（首选路线）

把看图得到的尺寸写成 spec JSON，交给 `spec_builder.py` 算出精确几何。

已验证的实例：`examples/link_03.spec.json`——连杆，左凸台 Ø36/Ø17、右凸台 Ø15/Ø8、中心距 53、上下由 R80 / R160 相切连接。**这些数字完全决定了形状**，所以实体是算出来的，不是拟合出来的：

| | 矢量化 | 按尺寸重建 |
|---|---|---|
| 实体数 | 35（碎片） | **12** |
| 比例 | 1 单位 = 1 像素（无意义） | **真实 mm** |
| 轮廓 | 断裂、含尺寸线 | **闭合** |
| 相切弧 | 拟合近似 | **数学精确** |

```bash
python scripts/spec_builder.py examples/link_03.spec.json -o link.dxf --preview link.png
```

spec 支持的实体：`circle` `line` `polyline` `arc` `arc3p` `tangent_arc` `bolt_circle` `centerline` `center_cross`。
坐标可以写成 `{"$polar": [[x,y], 角度, 距离]}` 或 `{"$mid": [[x1,y1],[x2,y2]]}`，这样 spec 里能直接写"从凸台中心 40mm、30°方向"，不用自己先算数。

**`tangent_arc` 是主力**：给定两个圆（`[x, y, 直径]`）、目标半径和 `modes`（`outer`/`inner`），它解出同时相切两圆的弧心与切点。摇臂、连杆、拨叉这类零件的外轮廓基本都是"两三个圆 + 相切弧"，几个实体就能闭合。

重建完**必须**渲染预览并用 `read_image` 亲眼看一遍，再和原图对照。看不出问题就等于没验证。

## 第 2 步：矢量化补缺口（补充路线）

数字没定下来的部分（自由曲线、图上没标的过渡）才交给矢量化：

```bash
python scripts/trace_image.py 图.jpg -o 输出目录 \
  --title "起重吊钩" --type "mechanical part" --tags "hook,mechanical" \
  --source photo --view single --width 150 \
  --vision '{"part_name":"lifting hook"}'
```

它会**同时跑 otsu 和 adaptive 两遍**，各写一份 dxf / ir.json / report.json / preview.png，再写 `compare.json`。

**为什么两遍都跑**：没有可靠的自动判据。实测十张图，九张的 otsu 墨占比在 15%~39%（看起来"过头了"），但在深色屏幕截图上**恰恰是 otsu 更好**（33 实体 vs adaptive 的 465）。墨占比规则和"轮廓/实体比"规则各会判错一个已知用例，margin 还很薄（5.1 vs 5.7）。所以脚本只给一个 provisional 结果，**最终由你看两张 `*.preview.png` 决定**。

看完之后，把更好的那个重新入库（记录按图片内容做键，会原地更新，不会重复堆积）：

```bash
python scripts/cad_library.py add --image 图.jpg --dxf 选中的.dxf \
  --ir 选中的.ir.json --report 选中的.report.json --preview 选中的.preview.png \
  --title "起重吊钩" --type "mechanical part" --source photo --view single \
  --params @params.json
```

> **PowerShell 陷阱**：`--params '{"a":1}'` 的内层引号会被 PowerShell 吃掉，报 `invalid JSON`。一律写成 `--params @文件.json`。中文参数（`--title`）没问题。

## 素材库

根目录 `$CAD_LIBRARY`，默认 `~/.dsh/cad-library`。纯文件，可拷贝、可 grep、可合并。

```
index.json              整个目录，一个 JSON 数组
records/<id>/           image / dxf / ir.json / report.json / preview.png / spec.json / meta.json
```

每条记录存：类型、来源、视图、看图得到的 `vision`（零件名/形状）、产生它的参数、几何指纹、**质量分与扣分理由**、以及重建用的 `spec`。

```bash
python scripts/cad_library.py stats
python scripts/cad_library.py list --type "mechanical part"
python scripts/cad_library.py search --image 新图.jpg          # 找相似的
python scripts/cad_library.py suggest --image 新图.jpg         # 该用什么参数
```

`suggest` 的复用有**硬门槛**，这是踩过坑改的：跨来源类别不借参数。把纸面照片的 `adaptive` 配方套到深色截图上，实体数从 33 炸到 461。所以配方只在"距离 < 0.4 **且** 来源类别相同"时才给，否则返回空并说明原因。**库只建议，实测说了算。**

## 已知边界（都实测过，别浪费时间去撞）

- **文字和尺寸不会被矢量化读出**。图上写着 Ø23，矢量结果里没有 Ø23——数字只能靠看图。这是走重建路线的根本原因。
- **尺寸线、箭头、标注会一起被矢量化**，混在轮廓里。这也是重建更干净的原因。
- **`adaptive` 在噪点图上会爆炸**：一张图的轮廓数能从 169 涨到 2666、实体从 33 涨到 465、耗时从 6 秒涨到 2 分钟。
- **手机拍屏幕**：摩尔纹、反光、屏幕外的亮背景会被算成"墨"（实测 otsu 判 25%~39%）。能拿到原截图就别用拍照。
- **曲线拟合能力有限**：非圆弧曲线（样条、椭圆、渐开线）只能拟合成弧或折线，`fit_rms` 会报残差。
- **多视图同屏**会被矢量成互相粘连的一坨。一次只处理一个视图。

## 装到别的机器

1. 把这个 `image-to-dxf` 目录整个拷到 `<DSH_HOME>/skills/`（默认 `~/.dsh/skills/`）或项目的 `.dsh/skills/` 下。目录自包含，只有 `SKILL.md` + `scripts/` + `examples/`。
2. 装 tracer：`pip install autocad-mcp`，或用一份 checkout（`uv sync`）。
3. 让脚本跑在有 `autocad_mcp` 的解释器上。脚本本身只用标准库 + `ezdxf` + `opencv`；`trace_image.py` 还需要 `autocad_mcp`：

   ```bash
   # 用 tracer 自己的 venv 跑，最稳
   /path/to/autocad-mcp/.venv/Scripts/python.exe scripts/trace_image.py ...
   ```

4. 可选：设 `CAD_LIBRARY` 指向自己的素材库目录；设 `DEEPSEEK_API_KEY` 可开启 tracer 的纯文本语义打标（它**看不到图**，只给已测出的几何起名，价值有限）。
