"""Schema for the *deep* knowledge layer of MinusDataOps.

The base framework (``minus_dataops.framework``) is the spine: six steps, each a
short, interview-ready summary. This package adds a deep dossier for each step --
multiple topics, technology comparisons with explicit pros/cons, numeric
heuristics, anti-patterns, what-breaks-at-scale notes, and the signals a strong
engineer gives. Every step's depth lives in its own module (``requirements.py``,
``pipeline.py``, ...), each exporting a single ``DEEP: DeepStep``.

All content stays tool- and cloud-agnostic: technologies are named as examples of
a category, never as the only valid choice. Use ASCII only (no emojis). Prefer
frozen tuples over lists so the knowledge is immutable.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Option:
    """One choice within a comparison (e.g. 'Parquet' vs 'Avro')."""

    name: str
    when: str                                   # when to pick this option
    pros: tuple[str, ...] = ()
    cons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Comparison:
    """A design fork and how to resolve it."""

    question: str                               # the decision being made
    options: tuple[Option, ...]
    rule_of_thumb: str = ""                      # one-line default


@dataclass(frozen=True)
class Heuristic:
    """A concrete, preferably numeric, rule of thumb."""

    name: str
    rule: str
    example: str = ""


@dataclass(frozen=True)
class Topic:
    """A deep sub-area of the step."""

    name: str
    summary: str
    points: tuple[str, ...] = ()                 # deep explanatory bullets
    examples: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeepStep:
    """The full deep dossier for one of the six framework steps."""

    step_key: str                                # must match framework.Step.key
    title: str
    overview: str
    topics: tuple[Topic, ...] = ()
    comparisons: tuple[Comparison, ...] = ()
    heuristics: tuple[Heuristic, ...] = ()
    pitfalls: tuple[str, ...] = ()               # anti-patterns / common mistakes
    at_scale: tuple[str, ...] = ()               # what breaks at TB/PB scale
    interview_signals: tuple[str, ...] = ()      # what a strong answer surfaces

    def concept_tokens(self) -> tuple[str, ...]:
        """Lowercase concept tokens this dossier covers (used by the coach scorer
        and search). Derived from topic names + comparison option names."""
        toks: list[str] = []
        for t in self.topics:
            toks.append(t.name.lower())
        for c in self.comparisons:
            for o in c.options:
                toks.append(o.name.lower())
        return tuple(dict.fromkeys(toks))         # de-duped, order-preserving


__all__ = ["Option", "Comparison", "Heuristic", "Topic", "DeepStep"]
