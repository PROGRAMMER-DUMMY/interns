# autoresearch — Test 1: Prompt Optimisation

> Copy this to `program.md` in repo root to activate this test.

---

## 1 · Identity

You are an autonomous prompt optimisation agent.
Your job is to improve a customer support system prompt so it scores higher on an LLM judge.

---

## 2 · Task definition

**Domain:** Prompt optimisation

**What you are optimising:**
Improve the system prompt in `tests/01_prompt/prompt.md` so that an LLM judge
rates its responses higher on helpfulness, clarity, and conciseness (combined score 0–10).

**Editable file:** `tests/01_prompt/prompt.md`

**Fixed files (do not modify):**
- `tests/01_prompt/evaluator.py`
- `tests/01_prompt/eval_cases.json`
- `task_runner.py`
- `program.md` (this file)

**Active interns:** Prompt Engineer Intern, Deep Research Intern, Eval Intern

---

## 3 · Metric

**Primary metric:** `primary_metric` (LLM judge score, 0.0–10.0)
**Direction:** higher is better
**Grep:** `grep "^primary_metric:" run.log`
**Secondary constraint:** `token_count` must stay below 800 tokens

**Three metric modes active for this test:**
- `primary_metric` — LLM judge score (main optimisation target)
- `rule_score` — rule-based: penalises passive voice, hedging words, sentences > 30 words
- `cost_score` — estimated cost per call (lower token count = lower cost)

All three are printed in the summary block. Optimise `primary_metric` primarily,
but note the other two in `results.tsv` description column.

---

## 4 · Setup

1. Run tag: `prompt-test-[today's date]`
2. Create branch: `git checkout -b autoresearch/prompt-test-[date]`
3. Read: this file, `tests/01_prompt/prompt.md`, `tests/01_prompt/evaluator.py`, `tests/01_prompt/eval_cases.json`
4. Initialise `results.tsv` with header only
5. Run baseline (prompt as-is)
6. Confirm, then loop

---

## 5 · Constraints

✅ May modify: `tests/01_prompt/prompt.md` only
❌ May not modify: evaluator, eval cases, task_runner, this file

---

## 6 · Experiment loop

Time budget per run: **30 seconds** (LLM calls are fast)
Hard timeout: 90 seconds

Deep research agent: invoke every **5 experiments** (not 10 — this is a fast domain)
Also invoke if 3 consecutive discards.

---

## 7 · Logging

```
commit	primary_metric	token_count	status	description
```

---

## 8 · Ideas bank (prompt-specific)

- Rewrite the persona (more direct, less corporate)
- Lead with the user's goal before the agent's constraints
- Replace passive voice with active voice throughout
- Add explicit output format instruction (e.g. "reply in 2–3 sentences")
- Add or remove few-shot examples
- Add chain-of-thought instruction ("think step by step before answering")
- Remove redundant constraints that are implied by others
- Tighten the tone instruction (vague "professional" → specific "direct and warm")
- Add a negative example ("do not say X")
- Reduce token count by cutting filler phrases