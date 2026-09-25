"""spec_builder - build a DXF from dimensions, not from pixels.

The tracer answers "where is the ink". This answers "where is the part", which
is the question a drawing actually encodes: Ø23, R60, 53, 128°. Those numbers
determine the geometry exactly, so the entities are *computed* (tangency,
concentricity, patterns) instead of being fitted to a photograph. The result is
true-to-scale, closed, and free of dimension lines.

A spec is JSON. Coordinates are millimetres in the drawing's own frame; the
first entity usually sets the datum.

    {
      "name": "连杆 link",
      "units": "mm",
      "layers": {"outline": "Thick", "center": "Center", "hidden": "Hidden"},
      "entities": [
        {"kind": "circle", "at": [0, 0], "diameter": 36, "layer": "outline"},
        {"kind": "circle", "at": [53, 0], "diameter": 15, "layer": "outline"},
        {"kind": "tangent_arc", "circle_a": [0, 0, 36], "circle_b": [53, 0, 15],
         "radius": 80, "pick": 1, "layer": "outline"},
        {"kind": "line", "from": [0, -18], "to": [53, -7.5], "layer": "outline"},
        {"kind": "centerline", "at": [0, 0], "span": 60, "angle": 0}
      ]
    }

Entity kinds
    circle          at [x,y], radius | diameter
    line            from [x,y] to [x,y]
    polyline        points [[x,y], ...], closed
    arc             center [x,y], radius, start_angle, end_angle (degrees, CCW)
    arc3p           through [ [x,y], [x,y], [x,y] ]
    tangent_arc     circle_a/circle_b as [x, y, diameter], radius, pick +1/-1,
                    modes ["outer"|"inner", "outer"|"inner"]
    fillet          between two lines [[p1,p2],[p3,p4]] with radius, pick +1/-1
    bolt_circle     center [x,y], count, pitch_diameter, hole_diameter
    polar           from [x,y], angle (deg), distance -> emits nothing; use it as
                    a coordinate via {"$polar": [[x,y], angle, distance]}
    centerline      at [x,y], span, angle  (a drawn construction line)
    center_cross    at [x,y], size        (the little cross a bore gets)

Any coordinate may be written as {"$polar": [[x, y], angle_deg, distance]} or
{"$mid": [[x1,y1],[x2,y2]]} so a spec can say "40 mm at 30° from the boss"
instead of pre-computing numbers.

Run:  python spec_builder.py spec.json -o out.dxf [--preview out.png] [--json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import ezdxf

DEFAULT_LAYERS = {"outline": "Thick", "center": "Center", "hidden": "Hidden", "thin": "Thin"}
LAYER_STYLE = {
    "Thick": ("CONTINUOUS", 50),
    "Thin": ("CONTINUOUS", 18),
    "Hidden": ("DASHED", 18),
    "Center": ("CENTER", 13),
    "Defpoints": ("CONTINUOUS", 13),
}


# ---------------------------------------------------------------------------
# tiny 2D geometry helpers
# ---------------------------------------------------------------------------


def circle_circle_intersection(c1, r1, c2, r2) -> list[tuple[float, float]]:
    """Both intersection points of two circles, or [] when they do not meet."""
    dx, dy = c2[0] - c1[0], c2[1] - c1[1]
    d = math.hypot(dx, dy)
    if d == 0 or d > r1 + r2 or d < abs(r1 - r2):
        return []
    a = (r1 * r1 - r2 * r2 + d * d) / (2 * d)
    h_sq = r1 * r1 - a * a
    h = math.sqrt(max(h_sq, 0.0))
    xm, ym = c1[0] + a * dx / d, c1[1] + a * dy / d
    rx, ry = -dy * (h / d), dx * (h / d)
    return [(xm + rx, ym + ry), (xm - rx, ym - ry)]


def resolve(value):
    """Evaluate a coordinate literal, including the $polar / $mid shorthands."""
    if isinstance(value, (list, tuple)):
        return [resolve(v) for v in value]
    if not isinstance(value, dict):
        return value
    if "$polar" in value:
        (base, angle, distance) = value["$polar"]
        base = resolve(base)
        rad = math.radians(angle)
        return [round(base[0] + distance * math.cos(rad), 6),
                round(base[1] + distance * math.sin(rad), 6)]
    if "$mid" in value:
        (a, b) = value["$mid"]
        a, b = resolve(a), resolve(b)
        return [round((a[0] + b[0]) / 2, 6), round((a[1] + b[1]) / 2, 6)]
    return value


def radius_of(spec: dict) -> float:
    if "radius" in spec:
        return float(spec["radius"])
    if "diameter" in spec:
        return float(spec["diameter"]) / 2.0
    raise ValueError("entity needs radius or diameter")


def tangent_arc(spec: dict) -> tuple[tuple[float, float], float, float, float] | None:
    """Arc of a given radius tangent to two circles.

    Returns (center, radius, start_angle, end_angle) or None when no tangency
    exists for the requested mode. Modes say how the arc meets each circle:
    "outer" keeps the centre R + r away (arc wraps outside the circle), "inner"
    keeps it |R - r| away (arc wraps around it).
    """
    (ax, ay, ad) = spec["circle_a"]
    (bx, by, bd) = spec["circle_b"]
    ra, rb = ad / 2.0, bd / 2.0
    radius = float(spec["radius"])
    modes = spec.get("modes", ["outer", "outer"])
    ra_eff = radius + ra if modes[0] == "outer" else abs(radius - ra)
    rb_eff = radius + rb if modes[1] == "outer" else abs(radius - rb)

    points = circle_circle_intersection((ax, ay), ra_eff, (bx, by), rb_eff)
    if not points:
        return None
    pick = 1 if float(spec.get("pick", 1)) >= 0 else -1
    center = points[0] if pick > 0 else points[1]

    def angle_to(target):
        return math.degrees(math.atan2(target[1] - center[1], target[0] - center[0]))

    # The arc runs between the two tangent points; choose the sweep that keeps
    # the arc on the same side as the requested pick.
    a1, a2 = angle_to((ax, ay)), angle_to((bx, by))
    if pick < 0:
        a1, a2 = a2, a1
    return center, radius, a1 % 360.0, a2 % 360.0


# ---------------------------------------------------------------------------
# builder
# ---------------------------------------------------------------------------


def ensure_layers(doc, layer_map: dict) -> None:
    if "CENTER" not in doc.linetypes:
        doc.linetypes.add("CENTER", pattern=[12.0, 4.0, -2.0, 4.0, -2.0])
    for name in set(layer_map.values()) | {"0"}:
        if name in doc.layers:
            continue
        linetype, lineweight = LAYER_STYLE.get(name, ("CONTINUOUS", 25))
        layer = doc.layers.add(name)
        layer.dxf.linetype = linetype
        try:
            layer.dxf.lineweight = lineweight
        except Exception:
            pass


def build(spec: dict):
    doc = ezdxf.new("R2010", setup=True)
    doc.units = ezdxf.units.MM
    layer_map = {**DEFAULT_LAYERS, **(spec.get("layers") or {})}
    ensure_layers(doc, layer_map)
    msp = doc.modelspace()

    def layer_of(entity: dict) -> str:
        return layer_map.get(entity.get("layer", "outline"), "Thick")

    emitted: list[dict] = []
    skipped: list[str] = []

    for item in spec.get("entities", []):
        kind = item.get("kind")
        layer = layer_of(item)

        if kind == "circle":
            at = resolve(item["at"])
            r = radius_of(item)
            msp.add_circle(at, r, dxfattribs={"layer": layer})
            emitted.append({"kind": "CIRCLE", "at": at, "r": r})
            if item.get("centerlines", False):
                msp.add_line((at[0] - 2 * r, at[1]), (at[0] + 2 * r, at[1]),
                             dxfattribs={"layer": layer_map["center"]})
                msp.add_line((at[0], at[1] - 2 * r), (at[0], at[1] + 2 * r),
                             dxfattribs={"layer": layer_map["center"]})

        elif kind == "line":
            start, end = resolve(item["from"]), resolve(item["to"])
            msp.add_line(start, end, dxfattribs={"layer": layer})
            emitted.append({"kind": "LINE", "from": start, "to": end})

        elif kind == "polyline":
            points = [resolve(p) for p in item["points"]]
            msp.add_lwpolyline(points, close=bool(item.get("closed", False)),
                               dxfattribs={"layer": layer})
            emitted.append({"kind": "LWPOLYLINE", "points": points})

        elif kind == "arc":
            center = resolve(item["center"])
            r = radius_of(item)
            msp.add_arc(center, r, float(item["start_angle"]), float(item["end_angle"]),
                        dxfattribs={"layer": layer})
            emitted.append({"kind": "ARC", "center": center, "r": r})

        elif kind == "arc3p":
            (p1, p2, p3) = [resolve(p) for p in item["through"]]
            try:
                center, r, a1, a2 = ezdxf.math.bulge_to_arc(None, None, None)  # placeholder
            except Exception:
                center = r = a1 = a2 = None
            # three-point circle
            ax, ay = p1
            bx, by = p2
            cx, cy = p3
            d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
            if abs(d) < 1e-9:
                skipped.append("arc3p: collinear points")
                continue
            ux = ((ax**2 + ay**2) * (by - cy) + (bx**2 + by**2) * (cy - ay) + (cx**2 + cy**2) * (ay - by)) / d
            uy = ((ax**2 + ay**2) * (cx - bx) + (bx**2 + by**2) * (ax - cx) + (cx**2 + cy**2) * (bx - ax)) / d
            center = (ux, uy)
            r = math.hypot(ax - ux, ay - uy)
            a1 = math.degrees(math.atan2(ay - uy, ax - ux))
            a2 = math.degrees(math.atan2(cy - uy, cx - ux))
            msp.add_arc(center, r, a1, a2, dxfattribs={"layer": layer})
            emitted.append({"kind": "ARC", "center": center, "r": r})

        elif kind == "tangent_arc":
            solved = tangent_arc(item)
            if solved is None:
                skipped.append(f"tangent_arc R{item.get('radius')}: no tangency to both circles")
                continue
            center, r, a1, a2 = solved
            msp.add_arc(center, r, a1, a2, dxfattribs={"layer": layer})
            emitted.append({"kind": "ARC", "center": list(center), "r": r,
                            "tangent": True, "start_angle": a1, "end_angle": a2})

        elif kind == "centerline":
            at = resolve(item["at"])
            span = float(item.get("span", 20))
            angle = math.radians(float(item.get("angle", 0)))
            dx, dy = math.cos(angle) * span / 2, math.sin(angle) * span / 2
            msp.add_line((at[0] - dx, at[1] - dy), (at[0] + dx, at[1] + dy),
                         dxfattribs={"layer": layer_map["center"]})
            emitted.append({"kind": "CENTERLINE", "at": at})

        elif kind == "center_cross":
            at = resolve(item["at"])
            size = float(item.get("size", 3))
            for (dx, dy) in ((size, 0), (0, size)):
                msp.add_line((at[0] - dx, at[1] - dy), (at[0] + dx, at[1] + dy),
                             dxfattribs={"layer": layer_map["center"]})
            emitted.append({"kind": "CENTER_CROSS", "at": at})

        elif kind == "bolt_circle":
            center = resolve(item["center"])
            count = int(item["count"])
            pitch_r = float(item["pitch_diameter"]) / 2.0
            hole_r = float(item["hole_diameter"]) / 2.0
            start = float(item.get("start_angle", 0))
            msp.add_circle(center, pitch_r, dxfattribs={"layer": layer_map["center"]})
            for index in range(count):
                angle = math.radians(start + index * 360.0 / count)
                at = (round(center[0] + pitch_r * math.cos(angle), 6),
                      round(center[1] + pitch_r * math.sin(angle), 6))
                msp.add_circle(at, hole_r, dxfattribs={"layer": layer})
                msp.add_line((at[0] - 2 * hole_r, at[1]), (at[0] + 2 * hole_r, at[1]),
                             dxfattribs={"layer": layer_map["center"]})
                msp.add_line((at[0], at[1] - 2 * hole_r), (at[0], at[1] + 2 * hole_r),
                             dxfattribs={"layer": layer_map["center"]})
                emitted.append({"kind": "CIRCLE", "at": list(at), "r": hole_r, "pattern": index})
            emitted.append({"kind": "BOLT_CIRCLE", "at": center, "count": count})

        elif kind == "fillet":
            skipped.append("fillet: not implemented yet - give the arc its centre and radius instead")
        else:
            skipped.append(f"unknown entity kind: {kind!r}")

    return doc, emitted, skipped


def render_preview(emitted: list[dict], preview_path: Path, title: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(figsize=(9, 9))
    for entity in emitted:
        if entity["kind"] == "LINE":
            axes.plot([entity["from"][0], entity["to"][0]],
                      [entity["from"][1], entity["to"][1]], "k-", lw=1.2)
        elif entity["kind"] == "CENTERLINE":
            continue
        elif entity["kind"] == "CIRCLE":
            circle = plt.Circle(entity["at"], entity["r"], fill=False, color="k", lw=1.2)
            axes.add_patch(circle)
        elif entity["kind"] == "ARC":
            center, r = entity["center"], entity["r"]
            if "start_angle" in entity:
                a1, a2 = entity["start_angle"], entity["end_angle"]
            else:
                a1, a2 = 0.0, 360.0
            steps = max(int(abs(a2 - a1)) + 2, 8)
            angles = [math.radians(a1 + (a2 - a1) * i / steps) for i in range(steps + 1)]
            axes.plot([center[0] + r * math.cos(a) for a in angles],
                      [center[1] + r * math.sin(a) for a in angles], "k-", lw=1.2)
    axes.set_aspect("equal")
    axes.grid(True, alpha=0.25, linestyle=":")
    axes.set_title(title)
    figure.savefig(preview_path, dpi=110, bbox_inches="tight")
    plt.close(figure)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(prog="spec_builder", description=__doc__.splitlines()[0])
    parser.add_argument("spec")
    parser.add_argument("-o", "--out", required=True)
    parser.add_argument("--preview")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    doc, emitted, skipped = build(spec)
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(out)

    if args.preview:
        render_preview(emitted, Path(args.preview), spec.get("name") or out.stem)

    report = {
        "name": spec.get("name"),
        "dxf": str(out),
        "entities": len(emitted),
        "by_kind": {kind: sum(1 for e in emitted if e["kind"] == kind)
                    for kind in sorted({e["kind"] for e in emitted})},
        "skipped": skipped,
        "scaled": True,
        "units": spec.get("units", "mm"),
    }
    print(json.dumps(report, ensure_ascii=False, indent=1) if args.json else
          f"{out}\nentities={report['entities']} {report['by_kind']}"
          + (f"\nskipped: {skipped}" if skipped else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
