"""cad_library - a local, self-contained material library for image -> DXF work.

Every drawing that goes through the tracer is filed here with the picture, the
DXF, the CAD-IR, the parameters that produced it and a small numeric
fingerprint. Later runs ask the library "have I seen something like this
before?" and reuse the parameters that worked, which is the concrete way a
library makes the next trace more accurate.

The library is plain files under one root, so it is copyable, greppable and
mergeable:

    <root>/index.json          one JSON array, the whole catalog
    <root>/records/<id>/       image / dxf / preview / ir.json / meta.json

Root resolution order: --root, $CAD_LIBRARY, ~/.dsh/cad-library.

CLI
    python cad_library.py add --image I.jpg --dxf I.dxf [--ir I.ir.json]
                              [--preview P.png] --title "..." [--type ...]
                              [--tags a,b] [--source photo] [--view single]
                              [--params '{"threshold":"adaptive"}'] [--notes ...]
    python cad_library.py search --image I.jpg [--top 5] [--json]
    python cad_library.py suggest --image I.jpg
    python cad_library.py list [--type ...] [--tag ...]
    python cad_library.py stats
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_ROOT = Path(os.environ.get("CAD_LIBRARY") or (Path.home() / ".dsh" / "cad-library"))

# Feature weights: how much each axis matters when judging "similar drawing".
FEATURE_WEIGHTS = {
    "lines": 1.0,
    "circles": 2.0,
    "arcs": 2.0,
    "round_ratio": 1.5,
    "aspect": 1.0,
    "ink_pct": 0.5,
}

# Beyond this distance a neighbour is not similar enough to lend its parameters.
REUSE_DISTANCE = 0.4


# --------------------------------------------------------------------------
# features
# --------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def features_from_trace(ir: dict, report: dict | None = None, params: dict | None = None) -> dict:
    """Reduce a trace to the handful of numbers that describe shape and trust.

    Two files describe one trace and they are NOT interchangeable: the CAD-IR
    (``*.ir.json``) carries ``entities``/``stats``/``layers``, while the
    diagnostics counters only exist in the runner's report (``*.report.json``).
    Reading the wrong one silently yields a perfect quality score.
    """
    report = report or {}
    stats = report.get("stats") or ir.get("stats") or {}
    counters = report.get("counters") or {}
    by_kind = stats.get("by_kind") or {}
    lines = int(by_kind.get("LINE", 0))
    circles = int(by_kind.get("CIRCLE", 0))
    arcs = int(by_kind.get("ARC", 0))
    polylines = int(by_kind.get("LWPOLYLINE", 0)) + int(by_kind.get("POLYLINE", 0))
    rounds = circles + arcs
    total = max(lines + rounds + polylines, 1)

    ink_pct = None
    ink_table = (params or {}).get("binarize_ink_pct")
    if isinstance(ink_table, dict):
        ink_pct = ink_table.get((params or {}).get("threshold"))
    if ink_pct is None:
        ink_pct = counters.get("ink_pct")

    width = ir.get("width") or 0
    height = ir.get("height") or 0
    entities = ir.get("entities") or []
    arc_rms = [e.get("fit_rms") for e in entities
               if isinstance(e, dict) and isinstance(e.get("fit_rms"), (int, float))]

    return {
        "lines": lines,
        "circles": circles,
        "arcs": arcs,
        "polylines": polylines,
        "round_ratio": round(rounds / total, 4),
        "aspect": round((width / height) if height else 0.0, 4),
        "ink_pct": round(float(ink_pct), 4) if isinstance(ink_pct, (int, float)) else None,
        "dangling": counters.get("dangling_endpoints"),
        "welds": counters.get("welded_endpoints"),
        "fit_rejected": counters.get("fit_rejected"),
        "contours": counters.get("contours"),
        "blobs_dropped": counters.get("blobs_dropped"),
        "chained_fragments": counters.get("chained_fragments"),
        "arc_fit_rms_max": round(max(arc_rms), 3) if arc_rms else None,
        "linetypes": counters.get("linetypes") or stats.get("by_linetype") or {},
        "layers": [layer.get("name") for layer in (ir.get("layers") or [])],
    }


def features_from_image(path: Path) -> dict:
    """A cheap fingerprint when no CAD-IR is available (search by a new picture)."""
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        return {"aspect": 0.0, "ink_pct": None}
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        return {"aspect": 0.0, "ink_pct": None}
    height, width = image.shape[:2]
    # Otsu over the whole picture is only a rough density probe here; the tracer
    # itself re-binarises with the threshold the caller chooses.
    _, binary = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    ink = float((binary > 0).mean() * 100.0)
    if ink > 50.0:
        ink = 100.0 - ink
    return {
        "aspect": round(width / height if height else 0.0, 4),
        "ink_pct": round(ink, 4),
        "pixels": width * height,
    }


def distance(a: dict, b: dict) -> float:
    """Weighted normalised L1 between two feature dicts (lower is closer)."""
    total = 0.0
    weight_sum = 0.0
    for key, weight in FEATURE_WEIGHTS.items():
        av, bv = a.get(key), b.get(key)
        if not isinstance(av, (int, float)) or not isinstance(bv, (int, float)):
            continue
        if key == "aspect":
            scale = 1.0
        elif key == "ink_pct":
            scale = 10.0
        elif key == "round_ratio":
            scale = 1.0
        else:
            scale = max(abs(av), abs(bv), 6.0)
        total += weight * min(abs(av - bv) / scale, 2.0)
        weight_sum += weight
    if weight_sum == 0.0:
        return 1.0
    return round(total / weight_sum, 4)


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------


def slugify(text: str) -> str:
    """ASCII-only slug. Returns "" for text with no ASCII - the caller decides the fallback."""
    return re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-").lower()[:40]


class Library:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else DEFAULT_ROOT
        self.records_dir = self.root / "records"
        self.index_path = self.root / "index.json"

    # -- io ---------------------------------------------------------------
    def load(self) -> list[dict]:
        if not self.index_path.exists():
            return []
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def save(self, records: list[dict]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(
            json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")

    # -- write ------------------------------------------------------------
    def add(
        self,
        image: Path,
        dxf: Path | None = None,
        ir: Path | None = None,
        report: Path | None = None,
        preview: Path | None = None,
        spec: Path | None = None,
        title: str = "",
        drawing_type: str = "unknown",
        tags: list[str] | None = None,
        source_kind: str = "unknown",
        view_kind: str = "unknown",
        params: dict | None = None,
        vision: dict | None = None,
        notes: str = "",
    ) -> dict:
        ir_report = _read_json(ir) if ir and Path(ir).exists() else {}
        diag_report = _read_json(report) if report and Path(report).exists() else {}
        features = features_from_trace(ir_report, diag_report, params)
        if not ir_report:
            features.update(features_from_image(Path(image)))

        digest = hashlib.sha1(Path(image).read_bytes()).hexdigest()[:10]
        # slugify() drops non-ASCII, so a Chinese title alone would collapse to
        # "drawing"; fall back to the file stem before giving up.
        label = slugify(title) or slugify(Path(image).stem) or "drawing"
        # Keyed on the picture's content, not the clock: re-tracing the same
        # image updates its record instead of piling up duplicates.
        record_id = f"{label}-{digest}"

        target = self.records_dir / record_id
        target.mkdir(parents=True, exist_ok=True)
        copied = {}
        for key, source in (("image", image), ("dxf", dxf), ("ir", ir),
                            ("report", report), ("preview", preview), ("spec", spec)):
            if source and Path(source).exists():
                destination = target / Path(source).name
                shutil.copyfile(source, destination)
                copied[key] = str(destination)

        previous = next((r for r in self.load() if r.get("id") == record_id), None)
        history = list(previous.get("history") or []) if previous else []
        if previous:
            history.append({
                "replaced_at": datetime.now().isoformat(timespec="seconds"),
                "params": previous.get("params"),
                "quality": previous.get("quality"),
                "threshold": (previous.get("params") or {}).get("threshold"),
            })

        spec_data = _read_json(spec) if spec and Path(spec).exists() else {}
        if spec_data:
            # A spec-built DXF has no tracer counters, so the counter-based
            # quality score would call it "no geometry recovered". It is the
            # opposite: every entity in it came from a number on the drawing.
            quality = {"score": 1.0,
                       "reasons": ["reconstructed from the drawing's dimensions - exact, not fitted"]}
        else:
            quality = quality_score(features, ir_report)

        record = {
            "id": record_id,
            "created": (previous or {}).get("created") or datetime.now().isoformat(timespec="seconds"),
            "updated": datetime.now().isoformat(timespec="seconds"),
            "title": title or Path(image).stem,
            "drawing_type": drawing_type,
            "source_kind": source_kind,
            "view_kind": view_kind,
            "tags": tags or [],
            "vision": vision or {},
            "notes": notes,
            "params": params or {},
            "spec": spec_data,
            "features": features,
            "quality": quality,
            "stats": (ir_report.get("stats") or diag_report.get("stats") or {}),
            "warnings": diag_report.get("warnings") or [],
            "image_sha1_10": digest,
            "files": copied,
            "history": history,
        }

        records = self.load()
        records = [r for r in records if r.get("id") != record_id]
        records.append(record)
        self.save(records)
        (target / "meta.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        return record

    # -- read -------------------------------------------------------------
    def search(
        self,
        image: Path | None = None,
        features: dict | None = None,
        drawing_type: str | None = None,
        tag: str | None = None,
        top: int = 5,
    ) -> list[dict]:
        query = features or (features_from_image(Path(image)) if image else {})
        scored = []
        for record in self.load():
            if drawing_type and record.get("drawing_type") != drawing_type:
                continue
            if tag and tag not in (record.get("tags") or []):
                continue
            score = distance(query, record.get("features") or {})
            scored.append({**record, "distance": score})
        scored.sort(key=lambda r: r["distance"])
        return scored[:top]

    def suggest(
        self,
        image: Path | None = None,
        features: dict | None = None,
        source_kind: str | None = None,
        top: int = 5,
    ) -> dict:
        """Reuse the parameters of the closest records - the point of the library.

        A recipe is only handed back when the closest record is genuinely close
        AND came from the same kind of source. Measured the hard way: reusing a
        paper photo's ``adaptive`` threshold on a dark screenshot turned 33
        entities into 461. Source class matters more than shape similarity for
        binarisation, so it is a hard gate, not a weight.
        """
        neighbours = self.search(image=image, features=features, top=top)
        if not neighbours:
            return {
                "recommended_params": {},
                "confidence": "none",
                "reason": "library is empty; using tracer defaults",
                "neighbours": [],
            }
        best = neighbours[0]
        if best["distance"] > REUSE_DISTANCE:
            return {
                "recommended_params": {},
                "confidence": "low",
                "reason": (f"closest record {best['id']} is too far (distance {best['distance']} "
                           f"> {REUSE_DISTANCE}); keeping measured defaults"),
                "neighbours": self._brief(neighbours),
            }
        if source_kind and source_kind != "unknown" and best.get("source_kind") not in (source_kind, "unknown"):
            return {
                "recommended_params": {},
                "confidence": "low",
                "reason": (f"closest record {best['id']} came from a {best.get('source_kind')}, "
                           f"not a {source_kind}; binarisation recipe would not transfer"),
                "neighbours": self._brief(neighbours),
            }

        best_params = best.get("params") or {}
        agreed = {}
        for key in ("threshold", "units", "min_line", "max_gap", "scale"):
            values = [n.get("params", {}).get(key) for n in neighbours]
            values = [v for v in values if v is not None]
            if not values:
                continue
            winner = max(set(map(str, values)), key=lambda v: sum(1 for x in values if str(x) == v))
            if sum(1 for x in values if str(x) == winner) >= 2:
                agreed[key] = winner
        confidence = "high" if best["distance"] < 0.15 else "medium"
        return {
            "recommended_params": {**best_params, **agreed},
            "confidence": confidence,
            "reason": (f"closest record {best['id']} (\"{best['title']}\") "
                       f"at distance {best['distance']}, same source class ({best.get('source_kind')})"),
            "neighbours": self._brief(neighbours),
        }

    @staticmethod
    def _brief(neighbours: list[dict]) -> list[dict]:
        return [
            {"id": n["id"], "title": n["title"], "distance": n["distance"],
             "drawing_type": n.get("drawing_type"), "source_kind": n.get("source_kind"),
             "params": n.get("params")}
            for n in neighbours
        ]


def quality_score(features: dict, ir: dict) -> dict:
    """A blunt 'is this trace trustworthy?' number, plus the reasons behind it."""
    reasons = []
    score = 1.0
    total = sum(int(features.get(k) or 0) for k in ("lines", "circles", "arcs", "polylines"))
    if total == 0:
        return {"score": 0.0, "reasons": ["no geometry recovered"]}

    dangling = features.get("dangling")
    if isinstance(dangling, (int, float)):
        ratio = dangling / total
        if ratio > 0.3:
            score -= 0.3
            reasons.append(f"{dangling} dangling endpoints vs {total} entities - the outline is broken")
        elif ratio > 0.15:
            score -= 0.15
            reasons.append(f"{dangling} dangling endpoints - some breaks in the outline")

    ink = features.get("ink_pct")
    if isinstance(ink, (int, float)):
        if ink > 20.0:
            score -= 0.35
            reasons.append(f"ink covers {ink:.1f}% of the page - the threshold is eating the background")
        elif ink > 8.0:
            score -= 0.15
            reasons.append(f"ink covers {ink:.1f}% of the page - background may be bleeding in")

    rejected = features.get("fit_rejected")
    contours = features.get("contours")
    if isinstance(rejected, (int, float)) and isinstance(contours, (int, float)) and contours > 0:
        ratio = rejected / contours
        if ratio > 0.3:
            score -= 0.2
            reasons.append(f"{rejected} of {contours} contours failed their circle/arc fit"
                           " - curved geometry was probably flattened into lines")

    curves_expected = features.get("round_ratio")
    if isinstance(curves_expected, (int, float)) and curves_expected < 0.05 and total >= 25:
        score -= 0.1
        reasons.append(f"only {features.get('circles', 0)} circle(s) + {features.get('arcs', 0)} arc(s) "
                       f"out of {total} entities - a busy drawing with almost no round geometry recovered")

    if not ir:
        score -= 0.1
        reasons.append("no CAD-IR stored; features came from the picture only")
    if not reasons:
        reasons.append("counters look consistent")
    return {"score": round(max(score, 0.0), 3), "reasons": reasons}


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def _loads(value: str) -> dict:
    """Parse a JSON argument, or read it from a file when written as @path.

    Shells mangle embedded quotes when handing JSON to a native command
    (PowerShell drops the inner double quotes outright), so the file form is the
    reliable way to pass a real object.
    """
    if value.startswith("@"):
        # utf-8-sig: Windows shells love writing a BOM, and json.loads rejects it.
        return json.loads(Path(value[1:]).read_text(encoding="utf-8-sig"))
    return json.loads(value)


def _library(args) -> Library:
    return Library(Path(args.root) if getattr(args, "root", None) else None)


def main(argv: list[str] | None = None) -> int:
    # A Windows console defaults to GBK and dies on the first "Ø" in a record.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(prog="cad_library", description=__doc__.splitlines()[0])
    parser.add_argument("--root", help=f"library root (default: {DEFAULT_ROOT})")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="file one traced drawing into the library")
    p_add.add_argument("--image", required=True)
    p_add.add_argument("--dxf")
    p_add.add_argument("--ir")
    p_add.add_argument("--report", help="the runner's diagnostics report (counters live here)")
    p_add.add_argument("--preview")
    p_add.add_argument("--spec", help="dimension spec JSON, when the part was reconstructed")
    p_add.add_argument("--title", default="")
    p_add.add_argument("--type", dest="drawing_type", default="unknown")
    p_add.add_argument("--tags", default="")
    p_add.add_argument("--source", default="unknown", help="photo | screenshot | render | scan")
    p_add.add_argument("--view", default="unknown", help="single | multi | iso")
    p_add.add_argument("--params", default="{}", help="JSON object, or @file.json")
    p_add.add_argument("--vision", default="{}", help="JSON object: what the picture showed, or @file.json")
    p_add.add_argument("--notes", default="")

    p_search = sub.add_parser("search", help="find records resembling an image")
    p_search.add_argument("--image", required=True)
    p_search.add_argument("--top", type=int, default=5)
    p_search.add_argument("--json", action="store_true")

    p_sug = sub.add_parser("suggest", help="parameters to reuse for this image")
    p_sug.add_argument("--image", required=True)
    p_sug.add_argument("--top", type=int, default=5)

    p_list = sub.add_parser("list", help="list records")
    p_list.add_argument("--type", dest="drawing_type")
    p_list.add_argument("--tag")

    sub.add_parser("stats", help="library summary")

    args = parser.parse_args(argv)
    library = _library(args)

    if args.command == "add":
        try:
            params = _loads(args.params)
            vision = _loads(args.vision)
        except json.JSONDecodeError as exc:
            print(f"invalid JSON: {exc}", file=sys.stderr)
            return 2
        record = library.add(
            image=Path(args.image), dxf=Path(args.dxf) if args.dxf else None,
            ir=Path(args.ir) if args.ir else None,
            report=Path(args.report) if args.report else None,
            preview=Path(args.preview) if args.preview else None,
            spec=Path(args.spec) if args.spec else None,
            title=args.title, drawing_type=args.drawing_type,
            tags=[t for t in args.tags.split(",") if t], source_kind=args.source,
            view_kind=args.view, params=params, vision=vision, notes=args.notes,
        )
        print(json.dumps(record, ensure_ascii=False, indent=1))
        return 0

    if args.command == "search":
        found = library.search(image=Path(args.image), top=args.top)
        if args.json:
            print(json.dumps(found, ensure_ascii=False, indent=1))
            return 0
        if not found:
            print("library is empty - nothing to compare against yet")
            return 0
        print(f"{'distance':<10}{'id':<52}{'title':<24}{'type':<18}quality")
        for record in found:
            print(f"{record['distance']:<10}{record['id']:<52}{record['title'][:22]:<24}"
                  f"{str(record.get('drawing_type'))[:16]:<18}{record.get('quality', {}).get('score')}")
        return 0

    if args.command == "suggest":
        print(json.dumps(library.suggest(image=Path(args.image), top=args.top),
                         ensure_ascii=False, indent=1))
        return 0

    if args.command == "list":
        records = library.load()
        if args.drawing_type:
            records = [r for r in records if r.get("drawing_type") == args.drawing_type]
        if args.tag:
            records = [r for r in records if args.tag in (r.get("tags") or [])]
        for record in records:
            print(f"{record['created']}  {record['id']}  {record['title']}")
        print(f"-- {len(records)} record(s) in {library.root}")
        return 0

    if args.command == "stats":
        records = library.load()
        kinds: dict[str, int] = {}
        tags: dict[str, int] = {}
        for record in records:
            kinds[record.get("drawing_type") or "unknown"] = kinds.get(record.get("drawing_type") or "unknown", 0) + 1
            for tag in record.get("tags") or []:
                tags[tag] = tags.get(tag, 0) + 1
        qualities = [r.get("quality", {}).get("score") for r in records
                     if isinstance(r.get("quality", {}).get("score"), (int, float))]
        print(json.dumps({
            "root": str(library.root),
            "records": len(records),
            "by_type": kinds,
            "top_tags": dict(sorted(tags.items(), key=lambda kv: -kv[1])[:10]),
            "mean_quality": round(sum(qualities) / len(qualities), 3) if qualities else None,
        }, ensure_ascii=False, indent=1))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
