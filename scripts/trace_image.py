"""trace_image - image -> DXF, with the binarisation question settled by measurement.

Why this wrapper exists instead of calling the tracer CLI directly:

1. **No threshold heuristic survives contact with real pictures.** Measured on
   ten example drawings, Otsu classified 15-39% of the page as ink on nine of
   them, yet Otsu still produced the *better* result on the dark screenshots
   (33 entities) while adaptive exploded to 465. The "ink share" rule and the
   "contours per entity" rule each get one of the two known cases wrong with a
   thin margin. So this script does not guess: it traces BOTH, writes both
   previews, and hands the choice to a pair of eyes.
2. **It still needs a default** for unattended use, so it picks provisionally by
   fragmentation (contours per emitted entity) and says so out loud.
3. **It files the run in the library**, so the shelf fills up by doing the work.

Run it with a Python that has autocad-mcp installed (its own venv is easiest);
see SKILL.md.

    python trace_image.py IMAGE [-o OUTDIR] [--title T] [--type "..."]
                                [--tags a,b] [--source photo] [--view single]
                                [--vision '{"part_name":"..."}']
                                [--threshold auto|otsu|adaptive]
                                [--width N] [--units mm] [--no-library]

With --threshold auto (the default) it writes `<stem>.otsu.*` and
`<stem>.adaptive.*` plus `compare.json`, and files the provisional winner.
Look at both `*.preview.png` files, then re-file the better one with
`cad_library.py add` - the record for that picture is updated in place.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from cad_library import Library, features_from_trace, features_from_image, quality_score  # noqa: E402

TRACER_DEFAULT = "otsu"


def probe_ink(image: Path) -> dict:
    """Report the ink share each threshold finds. Information, not a decision."""
    import cv2  # type: ignore

    from autocad_mcp.trace.vectorize import VectorizeOptions, binarize  # type: ignore

    gray = cv2.imread(str(image), cv2.IMREAD_GRAYSCALE)
    if gray is None:
        raise SystemExit(f"cannot read image: {image}")
    return {name: round(float((binarize(gray, VectorizeOptions(threshold=name)) > 0).mean() * 100.0), 3)
            for name in ("otsu", "adaptive")}


def fragmentation(features: dict) -> float | None:
    """Contours produced per emitted entity - how hard the binariser churned.

    A background that turned into ink yields many contours and few entities
    (13.2 measured); texture noise yields many of both (5.7). Lower is calmer.
    """
    total = sum(int(features.get(key) or 0) for key in ("lines", "circles", "arcs", "polylines"))
    contours = features.get("contours")
    if not total or not isinstance(contours, (int, float)):
        return None
    return round(contours / total, 3)


def run_one(image: Path, outdir: Path, threshold: str, args) -> dict:
    from autocad_mcp.trace.pipeline import TraceOptions, resolve_scale, trace_image  # type: ignore
    from autocad_mcp.trace.vectorize import VectorizeOptions  # type: ignore

    stem = f"{image.stem}.{threshold}"
    dxf_path = outdir / f"{stem}.dxf"
    ir_path = outdir / f"{stem}.ir.json"
    report_path = outdir / f"{stem}.report.json"
    preview_path = outdir / f"{stem}.preview.png"

    scale = resolve_scale(str(image), 1.0, args.width) if args.width else 1.0
    result = trace_image(str(image), TraceOptions(
        vectorize=VectorizeOptions(threshold=threshold, min_line_px=args.min_line,
                                   max_gap_px=args.max_gap, scale=scale, units=args.units),
        use_llm=args.use_llm,
        dxf_path=str(dxf_path), json_path=str(ir_path), preview_path=str(preview_path),
    ))
    if not result.ok:
        return {"threshold": threshold, "ok": False, "error": result.error}

    report = result.to_dict(include_ir=False, include_entities=False)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    ir = json.loads(ir_path.read_text(encoding="utf-8")) if ir_path.exists() else {}
    params = {"threshold": threshold, "units": args.units, "scale": scale, "width": args.width,
              "min_line": args.min_line, "max_gap": args.max_gap, "use_llm": args.use_llm}
    features = features_from_trace(ir, report, params)
    stats = ir.get("stats") or report.get("stats") or {}
    return {
        "threshold": threshold,
        "ok": True,
        "dxf": str(dxf_path), "ir": str(ir_path), "report": str(report_path), "preview": str(preview_path),
        "entities": stats.get("entities"),
        "by_kind": stats.get("by_kind"),
        "features": features,
        "quality": quality_score(features, ir),
        "fragmentation": fragmentation(features),
        "params": params,
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(prog="trace_image", description=__doc__.splitlines()[0])
    parser.add_argument("image")
    parser.add_argument("-o", "--outdir", default=None)
    parser.add_argument("--title", default="")
    parser.add_argument("--type", dest="drawing_type", default="unknown")
    parser.add_argument("--tags", default="")
    parser.add_argument("--source", default="unknown", help="photo | screenshot | render | scan")
    parser.add_argument("--view", default="unknown", help="single | multi | iso")
    parser.add_argument("--vision", default="{}", help="JSON: what the picture showed")
    parser.add_argument("--threshold", default="auto", choices=("auto", "otsu", "adaptive"))
    parser.add_argument("--width", type=float, default=None)
    parser.add_argument("--units", default="mm")
    parser.add_argument("--min-line", type=float, default=22.0)
    parser.add_argument("--max-gap", type=float, default=9.0)
    parser.add_argument("--use-llm", action="store_true", help="enable the text-only semantic pass")
    parser.add_argument("--no-library", action="store_true")
    parser.add_argument("--library-root", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    image = Path(args.image).resolve()
    if not image.exists():
        print(f"no such image: {image}", file=sys.stderr)
        return 2
    outdir = Path(args.outdir).resolve() if args.outdir else image.parent / (image.stem + "_dxf")
    outdir.mkdir(parents=True, exist_ok=True)

    ink = probe_ink(image)
    candidates = ["otsu", "adaptive"] if args.threshold == "auto" else [args.threshold]

    results = [run_one(image, outdir, threshold, args) for threshold in candidates]
    good = [r for r in results if r.get("ok")]
    if not good:
        print(json.dumps({"ok": False, "results": results}, ensure_ascii=False, indent=1), file=sys.stderr)
        return 1

    # Provisional winner: calmer fragmentation, with the tracer's own default
    # breaking ties (a stable, documented preference rather than an accident of
    # dictionary order).
    def rank(entry: dict) -> tuple:
        frag = entry.get("fragmentation")
        return (frag if isinstance(frag, (int, float)) else 1e9,
                0 if entry["threshold"] == TRACER_DEFAULT else 1)

    winner = sorted(good, key=rank)[0]

    try:
        vision = json.loads(args.vision)
    except json.JSONDecodeError:
        vision = {"summary": args.vision}

    library = None if args.no_library else Library(Path(args.library_root) if args.library_root else None)
    suggestion = {"recommended_params": {}, "confidence": "n/a", "reason": "library disabled"}
    record = None
    if library is not None:
        suggestion = library.suggest(features=features_from_image(image), source_kind=args.source)
        recap = {key: winner[key] for key in ("entities", "by_kind", "quality", "fragmentation")}
        record = library.add(
            image=image, dxf=Path(winner["dxf"]), ir=Path(winner["ir"]),
            report=Path(winner["report"]), preview=Path(winner["preview"]),
            title=args.title or image.stem, drawing_type=args.drawing_type,
            tags=[t for t in args.tags.split(",") if t], source_kind=args.source,
            view_kind=args.view, params={**winner["params"], "binarize_ink_pct": ink,
                                         "provisional": len(good) > 1},
            vision={**vision, "provisional_metrics": recap},
        )

    comparison = {
        "image": str(image),
        "ink_pct": ink,
        "candidates": results,
        "provisional_winner": winner["threshold"],
        "provisional_rule": "lowest contours-per-entity, tracer default breaks ties",
        "library_suggestion": suggestion,
        "library_record": (record or {}).get("id"),
        "next_step": ("look at each candidate's preview PNG, then re-file the best one with "
                      "cad_library.py add (the record is keyed on the picture, so it updates in place)"),
    }
    (outdir / "compare.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=1),
                                         encoding="utf-8")

    if args.json:
        print(json.dumps(comparison, ensure_ascii=False, indent=1))
    else:
        print(f"{image.name}  ink%: " + ", ".join(f"{k}={v}" for k, v in ink.items()))
        for entry in results:
            if not entry.get("ok"):
                print(f"  {entry['threshold']:<9} FAILED: {entry.get('error')}")
                continue
            mark = "<- provisional" if entry is winner else ""
            print(f"  {entry['threshold']:<9} entities={entry['entities']:<5} "
                  f"fragmentation={entry['fragmentation']:<8} quality={entry['quality']['score']} {mark}")
        print(f"compare: {outdir / 'compare.json'}")
        if record:
            print(f"filed as: {record['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
