"""``minus-dataops`` CLI -- query the framework, run decision rules, review a
project/directory, or drill the 6-step interview.

Subcommands
-----------
  explain [STEP]            Print the 6-step framework (or one step).
  decide  RULE [--set k=v]  Run a decision rule (batch_vs_stream, compute_engine,
                            storage_format, table_format, database_type,
                            modeling_shape, scd_type).
  volume  ...               Back-of-envelope math, or --scan a directory.
  review  [--root R] [--scan D] [--latency L]   Full system-design review.
  drill   [--scenario S] [--step K]             Print the interview drill.
  score   --answer TEXT|--answer-file F         Score an answer vs the framework.

The vendor stays self-contained: only ``review --root`` touches ``minus`` (to load
a generated project). Everything else is pure knowledge + rules.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from minus_dataops import coach, decisions
from minus_dataops import report as _report
from minus_dataops import volume as _volume
from minus_dataops.assess import review as _review
from minus_dataops.framework import FRAMEWORK, get_step, step_keys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass


# ---------------------------------------------------------------------------
# value coercion for `decide --set k=v`
# ---------------------------------------------------------------------------


def _coerce(v: str):
    low = v.strip().lower()
    if low in {"true", "yes", "y", "1"}:
        return True
    if low in {"false", "no", "n", "0"}:
        return False
    if low in {"none", "null", "unknown", ""}:
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


# ---------------------------------------------------------------------------
# deep-dossier rendering
# ---------------------------------------------------------------------------


def _deep_to_dict(deep) -> dict:
    return {
        "step_key": deep.step_key, "title": deep.title, "overview": deep.overview,
        "topics": [{"name": t.name, "summary": t.summary, "points": list(t.points),
                    "examples": list(t.examples)} for t in deep.topics],
        "comparisons": [{"question": c.question, "rule_of_thumb": c.rule_of_thumb,
                         "options": [{"name": o.name, "when": o.when,
                                      "pros": list(o.pros), "cons": list(o.cons)}
                                     for o in c.options]} for c in deep.comparisons],
        "heuristics": [{"name": h.name, "rule": h.rule, "example": h.example}
                       for h in deep.heuristics],
        "pitfalls": list(deep.pitfalls),
        "at_scale": list(deep.at_scale),
        "interview_signals": list(deep.interview_signals),
    }


def _print_deep(deep) -> None:
    print(f"\n=== Step deep-dive: {deep.title} ===")
    if deep.overview:
        print(deep.overview)
    if deep.topics:
        print("\n-- Topics --")
        for t in deep.topics:
            print(f"\n  {t.name}")
            print(f"    {t.summary}")
            for p in t.points:
                print(f"    - {p}")
            for ex in t.examples:
                print(f"    e.g. {ex}")
    if deep.comparisons:
        print("\n-- Comparisons --")
        for c in deep.comparisons:
            print(f"\n  {c.question}")
            for o in c.options:
                print(f"    [{o.name}] when: {o.when}")
                for pro in o.pros:
                    print(f"      + {pro}")
                for con in o.cons:
                    print(f"      - {con}")
            if c.rule_of_thumb:
                print(f"    rule of thumb: {c.rule_of_thumb}")
    if deep.heuristics:
        print("\n-- Heuristics --")
        for h in deep.heuristics:
            line = f"  * {h.name}: {h.rule}"
            print(line)
            if h.example:
                print(f"      e.g. {h.example}")
    if deep.pitfalls:
        print("\n-- Pitfalls / anti-patterns --")
        for p in deep.pitfalls:
            print(f"  [x] {p}")
    if deep.at_scale:
        print("\n-- What changes at TB/PB scale --")
        for a in deep.at_scale:
            print(f"  ~ {a}")
    if deep.interview_signals:
        print("\n-- Strong-answer signals --")
        for s in deep.interview_signals:
            print(f"  [ok] {s}")


# ---------------------------------------------------------------------------
# subcommand handlers
# ---------------------------------------------------------------------------


def _cmd_explain(args) -> int:
    if args.step:
        steps = [get_step(args.step)]
    else:
        steps = list(FRAMEWORK)

    if getattr(args, "deep", False):
        from minus_dataops import depth as _depth
        dossiers = [(s, _depth.get_deep(s.key)) for s in steps]
        if args.json:
            print(json.dumps([_deep_to_dict(d) for _s, d in dossiers if d], indent=2))
            return 0
        for s, d in dossiers:
            if d is None:
                print(f"\nStep {s.num}: {s.title} -- no deep dossier available yet.")
            else:
                _print_deep(d)
        return 0

    if args.json:
        payload = [{
            "num": s.num, "key": s.key, "title": s.title, "goal": s.goal,
            "sections": [{"title": sec.title, "points": list(sec.points)}
                         for sec in s.sections],
            "decisions": list(s.decisions), "pro_tip": s.pro_tip,
        } for s in steps]
        print(json.dumps(payload, indent=2))
        return 0
    for s in steps:
        print(f"\nStep {s.num}: {s.title}")
        print(f"  Goal: {s.goal}")
        for sec in s.sections:
            print(f"  {sec.title}:")
            for p in sec.points:
                print(f"    - {p}")
        if s.decisions:
            print(f"  Decisions made here: {', '.join(s.decisions)}")
        if s.pro_tip:
            print(f"  Pro-tip: {s.pro_tip}")
    return 0


def _cmd_deep(args) -> int:
    from minus_dataops import depth as _depth
    if args.list:
        avail = _depth.available()
        print(f"deep dossiers available ({len(avail)}/6): {', '.join(avail) or 'none'}")
        for key, d in _depth.deep_steps().items():
            print(f"  - {key}: {len(d.topics)} topics, {len(d.comparisons)} comparisons, "
                  f"{len(d.heuristics)} heuristics, {len(d.pitfalls)} pitfalls")
        return 0
    if args.step:
        steps = [get_step(args.step)]
    else:
        steps = list(FRAMEWORK)
    dossiers = [(s, _depth.get_deep(s.key)) for s in steps]
    if args.json:
        print(json.dumps([_deep_to_dict(d) for _s, d in dossiers if d], indent=2))
        return 0
    found = False
    for s, d in dossiers:
        if d is None:
            print(f"\nStep {s.num}: {s.title} -- no deep dossier available yet.")
        else:
            found = True
            _print_deep(d)
    return 0 if found else 1


# keywords that tie a decision rule to a deep-dossier comparison
_RULE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "batch_vs_stream": ("batch", "stream"),
    "compute_engine": ("single", "distributed", "node", "compute", "spark"),
    "storage_format": ("parquet", "avro", "columnar", "row"),
    "table_format": ("delta", "iceberg", "hudi", "table format", "open table"),
    "database_type": ("relational", "nosql", "document", "key-value", "wide-column"),
    "modeling_shape": ("star", "obt", "denormal", "snowflake", "one big table"),
    "scd_type": ("scd", "slowly changing", "type 2", "type 1"),
}


def _deep_comparison_for(rule_key: str):
    """Best-matching deep comparison(s) for a decision rule, searched across all
    dossiers. Returns a list (possibly empty) of (step_key, Comparison)."""
    kws = _RULE_KEYWORDS.get(rule_key, ())
    if not kws:
        return []
    from minus_dataops import depth
    scored = []
    for step_key, deep in depth.deep_steps().items():
        for c in deep.comparisons:
            q = c.question.lower()
            hits = sum(1 for k in kws if k in q)
            if hits:
                scored.append((hits, step_key, c))
    scored.sort(key=lambda t: t[0], reverse=True)
    if not scored:
        return []
    top = scored[0][0]
    return [(s, c) for h, s, c in scored if h == top]


def _deep_to_dict_comparison(step_key, c) -> dict:
    return {"step": step_key, "question": c.question, "rule_of_thumb": c.rule_of_thumb,
            "options": [{"name": o.name, "when": o.when, "pros": list(o.pros),
                         "cons": list(o.cons)} for o in c.options]}


def _print_comparison(step_key, c) -> None:
    print(f"\n    deep comparison (step: {step_key}): {c.question}")
    for o in c.options:
        print(f"      [{o.name}] when: {o.when}")
        for pro in o.pros:
            print(f"        + {pro}")
        for con in o.cons:
            print(f"        - {con}")
    if c.rule_of_thumb:
        print(f"      rule of thumb: {c.rule_of_thumb}")


def _cmd_decide(args) -> int:
    kwargs: dict = {}
    for item in args.set or []:
        if "=" not in item:
            print(f"[x] bad --set {item!r}; expected key=value", file=sys.stderr)
            return 2
        k, v = item.split("=", 1)
        kwargs[k.strip()] = _coerce(v)
    # convenience flags
    if args.latency is not None:
        kwargs.setdefault("latency", args.latency)
    if args.volume is not None:
        kwargs.setdefault("volume_per_day", args.volume)
    if args.workload is not None:
        kwargs.setdefault("workload", args.workload)
    try:
        d = decisions.decide(args.rule, **kwargs)
    except KeyError as exc:
        print(f"[x] {exc}", file=sys.stderr)
        return 2
    except TypeError as exc:
        print(f"[x] bad inputs for {args.rule}: {exc}", file=sys.stderr)
        return 2
    explain = getattr(args, "explain", False)
    if args.json:
        payload = dict(d.__dict__)
        if explain:
            payload["deep_comparisons"] = [
                _deep_to_dict_comparison(s, c) for s, c in _deep_comparison_for(d.key)
            ]
        print(json.dumps(payload, indent=2, default=str))
        return 0
    mark = _report._CONF_MARK.get(d.confidence, "[~]")
    print(f"{mark} {d.question}")
    print(f"    -> {d.recommendation}  (confidence: {d.confidence}, step: {d.step})")
    print(f"    {d.rationale}")
    for t in d.tradeoffs:
        print(f"    trade-off: {t}")
    if explain:
        matches = _deep_comparison_for(d.key)
        if matches:
            for s, c in matches:
                _print_comparison(s, c)
        else:
            print("    (no deep comparison mapped for this rule)")
    return 0


def _cmd_volume(args) -> int:
    if args.scan:
        est = _volume.estimate_from_path(args.scan)
    else:
        missing = [n for n in ("dau", "sessions", "events", "payload")
                   if getattr(args, n) is None]
        if missing:
            print(f"[x] back-of-envelope needs --{' --'.join(missing)} "
                  "(or use --scan DIR)", file=sys.stderr)
            return 2
        est = _volume.back_of_envelope(args.dau, args.sessions, args.events,
                                       args.payload, retention_days=args.retention)
    impl = _volume.implications(est)
    if args.json:
        print(json.dumps({"estimate": est.as_dict(),
                          "scale": impl["scale"],
                          "engine": impl["engine"].__dict__}, indent=2, default=str))
        return 0
    print(f"Volume estimate ({est.method}):")
    for k, v in est.human.items():
        print(f"  {k}: {v}")
    print(f"  scale: {impl['scale']}")
    eng = impl["engine"]
    print(f"  engine -> {eng.recommendation} ({eng.confidence})")
    fmts = (est.detail or {}).get("formats")
    if fmts:
        print("  formats on disk:")
        for fam, info in fmts.items():
            print(f"    - {fam}: {info['files']} files, {info['bytes']}")
    tf = (est.detail or {}).get("table_formats_detected")
    if tf:
        print(f"  table formats detected: {', '.join(tf)}")
    return 0


def _cmd_review(args) -> int:
    rv = _review(root=args.root, scan_path=args.scan, latency=args.latency,
                 subject=args.subject, deep=getattr(args, "deep", False))
    text = _report.to_json(rv) if args.json else _report.to_markdown(rv)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"[ok] DataOps review -> {args.out}")
        nq = sum(len(f.questions) for f in rv.findings)
        print(f"     subject: {rv.subject}; {len(rv.findings)} steps, "
              f"{nq} open question(s)")
    else:
        print(text)
    return 0


def _cmd_drill(args) -> int:
    steps = coach.drill(scenario=args.scenario, step=args.step)
    if args.json:
        print(json.dumps([{
            "step_key": d.step_key, "title": d.title, "prompts": list(d.prompts),
            "canonical": d.canonical, "common_miss": d.common_miss,
        } for d in steps], indent=2))
        return 0
    if args.scenario:
        print(f"Scenario: {args.scenario}\n")
    else:
        print("Scenarios you can practise: " +
              "; ".join(coach.scenarios()) + "\n")
    for d in steps:
        print(d.title)
        for p in d.prompts:
            print(f"  ? {p}")
        print(f"  [ok] strong answer: {d.canonical}")
        print(f"  [x] common miss: {d.common_miss}\n")
    return 0


def _cmd_score(args) -> int:
    if args.answer_file:
        text = Path(args.answer_file).read_text(encoding="utf-8")
    else:
        text = args.answer or ""
    if not text.strip():
        print("[x] provide --answer TEXT or --answer-file FILE", file=sys.stderr)
        return 2
    card = coach.score(text, step=args.step, deep=getattr(args, "deep", False))
    if args.json:
        print(json.dumps({
            "overall": round(card.overall, 3),
            "steps": [{"step": s.step_key, "fraction": round(s.fraction, 3),
                       "hit": s.hit, "missed": s.missed} for s in card.steps],
        }, indent=2))
        return 0
    print(f"Overall coverage: {card.overall:.0%}\n")
    for s in card.steps:
        mark = "[ok]" if s.fraction >= 0.6 else "[~]" if s.fraction >= 0.3 else "[x]"
        print(f"{mark} {s.title}: {s.fraction:.0%}")
        if s.missed:
            print(f"     missed: {', '.join(s.missed)}")
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="minus-dataops",
        description="Data-engineering system-design knowledge, decisions, reviews and drills.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("explain", help="Print the 6-step framework (or one step).")
    pe.add_argument("step", nargs="?", help=f"step number or key ({', '.join(step_keys())})")
    pe.add_argument("--deep", action="store_true",
                    help="print the deep dossier (topics, comparisons, heuristics, "
                         "pitfalls, at-scale notes) instead of the summary")
    pe.add_argument("--json", action="store_true")
    pe.set_defaults(func=_cmd_explain)

    pdp = sub.add_parser("deep", help="Print the deep dossier for a step (or all).")
    pdp.add_argument("step", nargs="?", help="step number or key")
    pdp.add_argument("--list", action="store_true", help="show which dossiers exist")
    pdp.add_argument("--json", action="store_true")
    pdp.set_defaults(func=_cmd_deep)

    pd = sub.add_parser("decide", help="Run a decision rule.")
    pd.add_argument("rule", help=f"one of: {', '.join(decisions.rule_keys())}")
    pd.add_argument("--set", action="append", metavar="key=value",
                    help="rule input, repeatable (e.g. --set needs_history=true)")
    pd.add_argument("--latency", help="convenience for batch_vs_stream (e.g. 5m, daily)")
    pd.add_argument("--volume", help="convenience for compute_engine (e.g. 4TB)")
    pd.add_argument("--workload", help="convenience for storage_format (e.g. streaming)")
    pd.add_argument("--explain", action="store_true",
                    help="append the matching deep comparison (options + pros/cons + "
                         "rule of thumb) that justifies the recommendation")
    pd.add_argument("--json", action="store_true")
    pd.set_defaults(func=_cmd_decide)

    pv = sub.add_parser("volume", help="Volume math (back-of-envelope or --scan).")
    pv.add_argument("--scan", help="directory to estimate footprint/formats from")
    pv.add_argument("--dau", type=float, help="daily active users")
    pv.add_argument("--sessions", type=float, help="sessions per user")
    pv.add_argument("--events", type=float, help="events per session")
    pv.add_argument("--payload", type=float, help="bytes per event")
    pv.add_argument("--retention", type=int, default=90, help="retention days (default 90)")
    pv.add_argument("--json", action="store_true")
    pv.set_defaults(func=_cmd_volume)

    pr = sub.add_parser("review", help="Full 6-step system-design review.")
    pr.add_argument("--root", help="generated MinusAnalyst project root (project.yaml)")
    pr.add_argument("--scan", help="data directory to estimate volume/formats from")
    pr.add_argument("--latency", help="known freshness SLA (e.g. 5m, daily)")
    pr.add_argument("--subject", help="override the review subject label")
    pr.add_argument("--deep", action="store_true",
                    help="enrich each step with deep what-breaks-at-scale notes + pitfalls")
    pr.add_argument("--out", help="write to this path instead of stdout")
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(func=_cmd_review)

    pdr = sub.add_parser("drill", help="Print the 6-step interview drill.")
    pdr.add_argument("--scenario", help="advisory framing (see `drill` output for ideas)")
    pdr.add_argument("--step", help="only this step (number or key)")
    pdr.add_argument("--json", action="store_true")
    pdr.set_defaults(func=_cmd_drill)

    ps = sub.add_parser("score", help="Score a free-text answer vs the framework.")
    ps.add_argument("--answer", help="answer text")
    ps.add_argument("--answer-file", help="read answer text from this file")
    ps.add_argument("--step", help="only score this step (number or key)")
    ps.add_argument("--deep", action="store_true",
                    help="also score coverage of concepts mined from the deep dossiers")
    ps.add_argument("--json", action="store_true")
    ps.set_defaults(func=_cmd_score)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
