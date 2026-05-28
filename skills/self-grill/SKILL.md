---
name: self-grill
description: >
  Use BEFORE you commit to a plan, design recommendation, or implementation
  approach. The skill turns the orchestrating agent into its own interviewer:
  it generates 3-6 grilling questions tailored to the current proposal,
  answers each with concrete evidence (file paths, samples, prior decisions),
  and surfaces any unknown-unknowns it can no longer brush past. Output is a
  short Self-Grill Audit block that goes into the response BEFORE the final
  recommendation.
argument-hint: "(optional) topic of the self-interrogation"
---

# Self-Grill

A reflexive critique step. Triggered explicitly by the user or implicitly
before any non-trivial recommendation (design choice, refactor proposal,
"the answer is X" statement). The agent generates and answers its own
grilling questions.

## When to invoke

Trigger this skill BEFORE producing a substantive answer to any of:

- "What's the best approach for X?"
- "Should I use A or B?"
- "Recommend the design for ..."
- "Pick the right way to handle ..."

Do NOT trigger for:

- Trivial lookups, single-fact answers, file reads.
- After the user has already chosen the path (their decision stands).
- When `/grill-me` is already running (it covers the interview phase).

## Procedure

Run all five steps in one turn. Do not stop between steps.

### Step 1 — Surface the proposal in one sentence

State, in one declarative sentence, the thing you are about to recommend.
"I'm about to recommend X." Force yourself to be concrete: if you can't
name what you'd recommend, the grilling has nothing to bite into and you
need to gather more evidence first.

### Step 2 — Generate 3–6 grilling questions

Pick questions from these archetypes that *actually apply* to the proposal.
Skip ones that don't.

1. **Hidden assumption**: "What am I assuming about the user's stack /
   data / scale / deployment model that I haven't verified?"
2. **Failure mode**: "What's the worst that happens if my recommendation
   is wrong, and how would the user discover it?"
3. **Reversibility**: "Is this a one-way door? If they change their mind
   in 6 months, what does undoing look like?"
4. **Edge case**: "What's the smallest input or scenario where my
   recommendation visibly breaks?"
5. **Alternative I dismissed**: "What did I rule out, and was the reason
   actually evidence or just preference?"
6. **Evidence freshness**: "Is anything I'm citing — file path, line
   number, feature flag, library version — possibly stale?"
7. **Concrete-enough test**: "What's the single observable signal that
   would prove this recommendation actually worked?"

### Step 3 — Answer each question concretely

For each question, write a 1-3 sentence answer. Cite file paths,
line numbers, profile samples, prior decisions, or memory entries. No
hand-waving like "the user probably wants ..." — if you can't cite, mark
the answer `UNVERIFIED:` and treat that as a flag.

### Step 4 — Tally the flags

Count answers that:

- Started with `UNVERIFIED:` (unknown that you tried to brush past).
- Revealed a failure mode worth telling the user about.
- Showed a reversibility cost the user might not see.

### Step 5 — Decide and emit the audit

If flag count is zero → proceed to the recommendation as planned.
If flag count is 1–2 → emit the recommendation, but call out the
flagged points in plain language at the bottom.
If flag count is 3+ → STOP. Do not recommend yet. Either gather
evidence to clear the flags, or ask the user for the missing input.

The audit output (Step 5) must be visible to the user in the response.
Do not hide it. The audit is the value.

## Output format

```markdown
### Self-Grill Audit

**Proposal**: <one sentence>

| Question | Answer | Flag? |
|---|---|---|
| ... | ... | yes/no |

**Verdict**: proceed | proceed-with-caveats | stop-and-clarify

<then the actual recommendation, or the request for more input>
```

## Combining with other skills

- `/grill-me` interviews THE USER. `/self-grill` interviews THE AGENT.
  Use both: `/grill-me` clears stakeholder ambiguity; `/self-grill`
  clears the agent's own blind spots before committing.
- After `/architect`, run `/self-grill` to sanity-check the PRD before
  handing to `/builder`.
- After a long-running session, `/self-grill` plus `/handoff` is the
  natural close: grill yourself for unknown-unknowns, then write the
  handoff so the next agent inherits clean state.

## Anti-patterns to refuse

- **Fake grilling**: writing questions that you obviously already know
  the answer to so the audit looks clean. If the question doesn't have
  a real risk of flagging something, skip it.
- **Grilling theater**: emitting the audit table but ignoring its
  verdict. If 3+ flags fire, you MUST stop.
- **Recursive grilling**: do not invoke `/self-grill` from inside a
  `/self-grill` run. One pass per recommendation.
