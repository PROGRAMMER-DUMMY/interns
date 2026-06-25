"""Export screener: build, rasterize, and check the dashboard export artifacts
(the MinusDeck ``.pptx`` and MinusPress ``.pdf`` vendors), then stage every
rendered page for the orchestrating agent's vision review.

This is the export-side equivalent of ``core.dashboard.screener`` (which screens
the LIVE Dash app). Same shape:

1. (re)build the certified MinusAnalyst project, then build the requested
   export(s) into ``interns/reports/dashboard_export_screener/<kind>.<ext>``,
2. RASTERIZE each output to per-page PNGs:
     * PDF  -> pdfplumber renders each page,
     * PPTX -> LibreOffice ``soffice`` converts to PDF, then pdfplumber renders
       it (graceful warning when soffice is absent -- structural checks still run),
3. run DETERMINISTIC checks -- file present + non-trivial; expected slide/page,
   chart, native-chart + embedded-Excel (pptx) and outline/link (pdf) counts;
   every rendered page is a valid, non-blank PNG; palette accent-vs-paper delta-E,
4. write ``current.json`` + ``current.md`` with the findings + a ``vision_review``
   gate that starts PENDING (flipped by ``--record-vision-review`` with
   provenance, exactly like the live screener).

The SUBJECTIVE pass (does it look like the dashboard? labels clipped? colours
off?) is the agent's job: the report lists every staged PNG to Read and judge.
"""
from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

REPORT_SUBDIR = ("interns", "reports", "dashboard_export_screener")

_SOFFICE_CANDIDATES = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/usr/bin/soffice",
    "/usr/local/bin/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
)


def _soffice_bin() -> Optional[str]:
    for cand in _SOFFICE_CANDIDATES:
        if Path(cand).is_file():
            return cand
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _png_size(path: Path) -> Optional[tuple[int, int]]:
    try:
        with path.open("rb") as fh:
            head = fh.read(24)
        if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
            return None
        w, h = struct.unpack(">II", head[16:24])
        return int(w), int(h)
    except (OSError, struct.error):
        return None


def _looks_blank(path: Path) -> bool:
    """True when a rendered page is effectively empty (a failed render = one flat
    colour). Measures the fraction of non-background "ink" pixels via PIL on a
    downscaled copy -- robust for content-sparse-but-valid pages (a cover or
    contents page still carries text/rules well above the threshold, whereas a
    blank frame is ~0% ink with only a handful of colours). Falls back to a
    coarse file-size test when PIL is unavailable."""
    try:
        from PIL import Image
    except Exception:
        try:
            return path.stat().st_size < 2500
        except OSError:
            return True
    try:
        im = Image.open(path).convert("RGB")
        if im.width > 240:
            im = im.resize((240, max(1, round(240 * im.height / im.width))))
        colors = im.getcolors(maxcolors=1 << 20)  # (count, rgb) pairs, no deprecation
        if not colors:
            return False                          # too many colours = clearly not blank
        total = sum(c for c, _ in colors)
        if not total:
            return True
        modal = max(colors, key=lambda cv: cv[0])[1]
        ink = sum(c for c, rgb in colors
                  if abs(rgb[0] - modal[0]) + abs(rgb[1] - modal[1])
                  + abs(rgb[2] - modal[2]) > 40)
        # Sparsest valid page (a cover) measured ~3.7% ink / 700+ colours; a flat
        # frame is well under 0.4% with very few colours.
        return (ink / total) < 0.004 and len(colors) < 60
    except Exception:
        return False


# ---------------------------------------------------------------------------
# rasterization
# ---------------------------------------------------------------------------


def _raster_pdf(pdf_path: Path, out_dir: Path, stem: str, *, dpi: int = 110) -> list[Path]:
    """Render each PDF page to ``<stem>_pNN.png``. Returns the written paths."""
    import pdfplumber

    shots: list[Path] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for i, page in enumerate(pdf.pages):
            out = out_dir / f"{stem}_p{i:02d}.png"
            page.to_image(resolution=dpi).save(str(out))
            shots.append(out)
    return shots


def _pptx_to_pdf_powerpoint(pptx_path: Path, out_dir: Path) -> tuple[Optional[Path], str]:
    """Convert .pptx -> .pdf through Microsoft PowerPoint via COM (Windows + Office).

    This uses PowerPoint's OWN renderer, so native Excel-backed charts rasterize
    exactly as a user sees them -- higher fidelity than any third-party converter.
    (None, reason) when PowerPoint/pywin32 are unavailable or automation fails.
    """
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except Exception:
        return None, "pywin32 not available"

    # PowerPoint COM resolves paths against its own working dir, not ours, so it
    # fails (0x80070003 PATH_NOT_FOUND) on relative paths -- always pass absolute.
    src = str(pptx_path.resolve())
    pdf_path = (out_dir.resolve() / (pptx_path.stem + "_ppt.pdf"))
    pythoncom.CoInitialize()
    app = None
    pres = None
    try:
        app = win32com.client.Dispatch("PowerPoint.Application")
        # ReadOnly + no window: open headless-ish without touching the source.
        pres = app.Presentations.Open(src, ReadOnly=True,
                                       Untitled=False, WithWindow=False)
        pres.SaveAs(str(pdf_path), 32)   # 32 = ppSaveAsPDF
        pres.Close()
        pres = None
    except Exception as exc:
        return None, f"PowerPoint COM failed: {exc}"
    finally:
        try:
            if pres is not None:
                pres.Close()
        except Exception:
            pass
        try:
            if app is not None:
                app.Quit()
        except Exception:
            pass
        pythoncom.CoUninitialize()
    if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
        return None, "PowerPoint produced no PDF"
    return pdf_path, ""


def _pptx_to_pdf_soffice(pptx_path: Path, out_dir: Path) -> tuple[Optional[Path], str]:
    """Convert .pptx -> .pdf via LibreOffice headless (cross-platform fallback)."""
    soffice = _soffice_bin()
    if not soffice:
        return None, "LibreOffice 'soffice' not found"
    try:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir",
             str(out_dir), str(pptx_path)],
            capture_output=True, timeout=180,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, f"soffice conversion failed: {exc}"
    pdf_path = out_dir / (pptx_path.stem + ".pdf")
    if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
        return None, "soffice produced no PDF"
    return pdf_path, ""


def _pptx_to_pdf(pptx_path: Path, out_dir: Path) -> tuple[Optional[Path], str]:
    """Convert a .pptx to .pdf for rasterization. Prefers PowerPoint (real engine,
    best fidelity) and falls back to LibreOffice. (pdf_path, '') on success;
    (None, reason) when neither backend is available/works."""
    reasons: list[str] = []
    for backend in (_pptx_to_pdf_powerpoint, _pptx_to_pdf_soffice):
        pdf_path, reason = backend(pptx_path, out_dir)
        if pdf_path is not None:
            return pdf_path, ""
        reasons.append(reason)
    return None, ("no PPTX->image backend available (" + "; ".join(reasons)
                  + "). Install Microsoft PowerPoint, or LibreOffice via "
                  "`winget install TheDocumentFoundation.LibreOffice`.")


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------


@dataclass
class ArtifactFinding:
    kind: str                                   # pptx | pdf
    path: str = ""
    summary: dict[str, Any] = field(default_factory=dict)
    shots: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------------
# per-artifact structural checks (on the actual emitted file)
# ---------------------------------------------------------------------------


def _check_pptx_structure(path: Path, res: dict, f: ArtifactFinding) -> None:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
    charts = [n for n in names if re.fullmatch(r"ppt/charts/chart\d+\.xml", n)]
    embeds = [n for n in names if n.startswith("ppt/embeddings/") and n.endswith(".xlsx")]
    slides = [n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
    f.summary.update({"slides": len(slides), "native_charts": len(charts),
                      "embedded_xlsx": len(embeds),
                      "reported_native": res.get("native_charts"),
                      "reported_image": res.get("image_charts")})
    if len(slides) != res.get("slides"):
        f.warnings.append(f"slide count {len(slides)} != reported {res.get('slides')}")
    if res.get("charts_mode") == "native":
        if res.get("native_charts", 0) and not charts:
            f.errors.append("native mode reported native charts but none embedded in the file")
        if len(embeds) < len(charts):
            f.errors.append(f"{len(charts)} native charts but only {len(embeds)} embedded "
                            "Excel workbooks (charts not editable)")


def _check_pdf_structure(path: Path, res: dict, f: ArtifactFinding) -> None:
    raw = path.read_bytes()
    has_outline = b"/Outlines" in raw
    titles = len(re.findall(rb"/Title", raw))
    links = len(re.findall(rb"/Subtype\s*/Link", raw))
    f.summary.update({"charts": res.get("charts"), "tables": res.get("tables"),
                      "kpis": res.get("kpis"), "outline": has_outline,
                      "outline_titles": titles, "link_annotations": links})
    if not has_outline:
        f.errors.append("PDF has no outline/bookmarks (not navigable)")
    if links == 0:
        f.errors.append("PDF has no clickable contents links")


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------


def screen_exports(repo_root: Path, workspace_rel: str, *,
                   kinds: tuple[str, ...] = ("pptx", "pdf"),
                   charts: str = "native", theme: Optional[str] = None,
                   force: bool = False) -> dict[str, Any]:
    """Build + rasterize + check the requested export artifacts; write the report."""
    from tools.dashboard_export_common import add_vendor_to_path, prepare_minus_root
    from tools.dashboard_verify import _delta_e

    repo_root = Path(repo_root).resolve()
    workspace = (repo_root / workspace_rel).resolve()
    report_dir = workspace.joinpath(*REPORT_SUBDIR)
    shots_dir = report_dir / "shots"
    shots_dir.mkdir(parents=True, exist_ok=True)

    findings: list[ArtifactFinding] = []

    # Build the certified project once (shared by both artifacts).
    try:
        _layout, root, _gen, warns = prepare_minus_root(
            repo_root, workspace_rel, force=force)
    except (FileNotFoundError, RuntimeError) as exc:
        findings.append(ArtifactFinding(kind="(build)", errors=[str(exc)]))
        return _write_report(repo_root, workspace, workspace_rel, report_dir,
                             shots_dir, findings, [])

    add_vendor_to_path(repo_root)

    for kind in kinds:
        f = ArtifactFinding(kind=kind)
        for w in warns:
            f.warnings.append(w)
        ext = kind
        out_path = report_dir / f"{kind}.{ext}"
        try:
            if kind == "pptx":
                from minus_deck import build_deck
                res = build_deck(root, out_path, theme=theme, charts=charts)
            elif kind == "pdf":
                from minus_press import build_pdf
                res = build_pdf(root, out_path, theme=theme)
            else:
                f.errors.append(f"unknown export kind {kind!r}")
                findings.append(f)
                continue
        except Exception as exc:  # a build crash is a hard finding
            f.errors.append(f"build failed: {exc}")
            findings.append(f)
            continue

        f.path = str(out_path.relative_to(workspace).as_posix())
        f.summary["bytes"] = out_path.stat().st_size
        if out_path.stat().st_size < 20_000:
            f.errors.append("export file is implausibly small")

        # structural checks on the real file
        try:
            if kind == "pptx":
                _check_pptx_structure(out_path, res, f)
            else:
                _check_pdf_structure(out_path, res, f)
        except Exception as exc:
            f.errors.append(f"structural check failed: {exc}")

        # rasterize for the visual pass
        try:
            if kind == "pdf":
                shots = _raster_pdf(out_path, shots_dir, "pdf")
            else:
                pdf_path, reason = _pptx_to_pdf(out_path, shots_dir)
                if pdf_path is None:
                    f.warnings.append(f"visual pass skipped: {reason}")
                    shots = []
                else:
                    shots = _raster_pdf(pdf_path, shots_dir, "pptx")
                    pdf_path.unlink(missing_ok=True)  # keep only the PNGs
        except Exception as exc:
            f.warnings.append(f"rasterization failed: {exc}")
            shots = []

        for shot in shots:
            size = _png_size(shot)
            if size is None:
                f.errors.append(f"rendered page {shot.name} is not a valid PNG")
            elif _looks_blank(shot):
                f.errors.append(f"rendered page {shot.name} is nearly blank")
            else:
                f.shots.append(str(shot.relative_to(workspace).as_posix()))
        findings.append(f)

    # palette sanity (shared design tokens): accent must separate from paper
    palette_findings: list[str] = []
    try:
        from core.dashboard.design_md import load_design_tokens

        tokens = load_design_tokens(workspace)
        de = _delta_e(tokens.accent, tokens.paper)
        if de is not None and de < 20:
            palette_findings.append(
                f"accent {tokens.accent} vs paper {tokens.paper} delta-E {de:.1f} (<20): "
                "charts may not stand out in the export")
    except Exception:
        pass

    # Deterministic measure-semantics guard (shared with the live screener):
    # format/aggregation must match the metric -- no currency-on-count, no summed
    # averages/shares. Hard findings; fail the gate without the vision review.
    from core.dashboard.screener import _check_measure_semantics

    semantic_findings = _check_measure_semantics(root)

    return _write_report(repo_root, workspace, workspace_rel, report_dir,
                         shots_dir, findings, palette_findings, semantic_findings)


def _write_report(repo_root, workspace, workspace_rel, report_dir, shots_dir,
                  findings, palette_findings, semantic_findings=None) -> dict[str, Any]:
    semantic_findings = semantic_findings or []
    ok = all(f.ok for f in findings) and not palette_findings and not semantic_findings
    all_shots = [s for f in findings for s in f.shots]
    report = {
        "artifact_type": "dashboard_export_screener/current.json",
        "version": 1,
        "workspace": workspace_rel,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ok": ok,
        "artifact_count": len(findings),
        "error_count": (sum(len(f.errors) for f in findings)
                        + len(palette_findings) + len(semantic_findings)),
        "palette_findings": palette_findings,
        "measure_semantics_findings": semantic_findings,
        "vision_review": {"status": "pending", "shots": all_shots},
        "artifacts": [
            {"kind": f.kind, "path": f.path, "ok": f.ok, "summary": f.summary,
             "shots": f.shots, "errors": f.errors, "warnings": f.warnings}
            for f in findings
        ],
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "current.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Dashboard Export Screener (MinusDeck / MinusPress)",
        "",
        f"- Workspace: `{workspace_rel}`",
        f"- Artifacts: {len(findings)}  |  Status: {'[ok]' if ok else '[x] findings below'}",
        "",
    ]
    for f in findings:
        marker = "[ok]" if f.ok else "[x]"
        lines.append(f"## {marker} {f.kind}  `{f.path}`")
        if f.summary:
            lines.append("- " + "  ".join(f"{k}={v}" for k, v in f.summary.items()))
        for e in f.errors:
            lines.append(f"- [x] {e}")
        for w in f.warnings:
            lines.append(f"- [~] {w}")
        if f.shots:
            lines.append(f"- rendered pages ({len(f.shots)}):")
            lines.extend(f"  - `{s}`" for s in f.shots)
        lines.append("")
    if semantic_findings:
        lines.append("## [x] Measure semantics")
        lines.extend(f"- [x] {s}" for s in semantic_findings)
        lines.append("")
    if palette_findings:
        lines.append("## [x] Palette")
        lines.extend(f"- [x] {p}" for p in palette_findings)
        lines.append("")
    lines += [
        "## Vision review (agent step)",
        "",
        "Deterministic checks cannot judge aesthetics. The reviewing agent should",
        "Read each rendered page above and check: it matches the live dashboard's",
        "look (warm paper, terracotta accent, serif headings); charts carry their",
        "value labels and are not clipped/overlapping; KPI cards show plausible",
        "headline numbers; tables are readable; nothing renders as an empty frame.",
        "Then run `--record-vision-review` to clear the gate.",
        "",
    ]
    (report_dir / "current.md").write_text("\n".join(lines), encoding="utf-8")
    return {
        "ok": ok,
        "report_json": str((report_dir / "current.json").relative_to(repo_root).as_posix()),
        "report_md": str((report_dir / "current.md").relative_to(repo_root).as_posix()),
        "screenshot_dir": str(shots_dir.relative_to(repo_root).as_posix()),
        "artifact_count": len(findings),
        "error_count": report["error_count"],
    }


def record_vision_review(repo_root: Path, workspace_rel: str, *,
                         reviewed_by: str = "", notes: str = "") -> dict[str, Any]:
    """Flip the export screener's vision_review to done, with provenance (empty
    reviewer -> source: agent; a name -> source: human). Mirrors the live screener
    and the repo's Human-Gate Provenance Rule."""
    report_path = (Path(repo_root).resolve() / workspace_rel).joinpath(
        *REPORT_SUBDIR, "current.json")
    if not report_path.exists():
        raise FileNotFoundError(f"no export screener report to review: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    review = report.get("vision_review") or {}
    review.update({
        "status": "done",
        "reviewed_by": reviewed_by,
        "source": "human" if reviewed_by else "agent",
        "notes": notes,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    })
    report["vision_review"] = review
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return review


__all__ = ["screen_exports", "record_vision_review"]
