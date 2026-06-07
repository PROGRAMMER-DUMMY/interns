---
name: kpi-clarification
description: >
  Converts ambiguous or loosely written KPI descriptions into precise, unambiguous business metric definitions.
  Use this skill whenever a user mentions a KPI, metric, business measure, or indicator that needs to be defined, clarified, structured, or documented — even if they don't use the word "KPI" explicitly.
  Trigger examples: "define this metric", "what does this KPI mean", "help me document our conversion rate", "clarify this measure for our BI team", "we track X, can you write it up properly", "our dashboard shows Y, not sure what it means", "turn this into a proper metric definition".
  Always use this skill when the user presents any business performance metric, OKR component, or analytics measure that needs structured decomposition.
---

# KPI Clarification Skill

## Objective

Convert ambiguous KPI descriptions into clear, unambiguous, business-ready metric definitions. Every output must leave zero room for misinterpretation — a new team member should be able to read it and build the exact same metric independently.

---

## Step-by-Step Decomposition Process

For **every KPI** provided, work through each of these dimensions in order:

### 1. Metric Being Measured
Identify the core business concept. What is the subject of measurement? (e.g., customers, revenue, orders, sessions, tickets)

### 2. Numerator
What is being counted or summed in the top of the calculation? Be explicit about what qualifies — include any implicit conditions embedded in the KPI name.

### 3. Denominator *(if applicable)*
What is the total population or base being divided against? If none applies (e.g., for a simple count or total), state "N/A".

### 4. Aggregation Function
Identify the mathematical operation used:
- `SUM` — total of values
- `COUNT` — number of records
- `COUNT DISTINCT` — unique entities only
- `AVG` — arithmetic mean
- `MIN` / `MAX` — extreme values
- `MEDIAN` — middle value in distribution
- `RATIO` / `PERCENTAGE` — numerator ÷ denominator × 100

### 5. Grouping Dimensions
Which attributes would this metric typically be sliced by?
Examples: Department, Product, Region, Customer Segment, Channel, Date, Provider, SKU, Employee, Store

Only include dimensions **explicitly stated or strongly implied** by the KPI. Do not invent dimensions.

### 6. Filters & Conditions
What subsets or exclusions apply?
Examples: "only active users", "excluding refunds", "status = 'completed'", "revenue > 0", "B2B segment only"

If no filters are stated, flag this as an ambiguity.

### 7. Time Grain
What is the reporting frequency or period?
- Daily / Weekly / Monthly / Quarterly / Yearly
- Rolling (e.g., Rolling 30-day, Rolling 12-month)
- Point-in-time snapshot

If not stated, flag this as a clarification question.

### 8. Output Type
Classify the final output:

| Output Type | Description |
|-------------|-------------|
| **Count** | Whole number of records/entities |
| **Total** | Sum of a numeric value (e.g., revenue $) |
| **Average** | Mean value per entity or period |
| **Percentage** | Part-to-whole ratio × 100 |
| **Ratio** | Part-to-whole as a decimal (e.g., 0.23) |
| **Rate** | Occurrence per unit of time or population |
| **Index** | Normalized benchmark comparison |
| **Score** | Composite or weighted multi-factor measure |

---

## Output Format

Always structure your response exactly as follows:

```
---
Original KPI:
[Paste the exact input KPI as stated by the user]

Business Definition:
[Plain-English explanation of what this metric measures and why it matters to the business]

Calculation Logic:
[Numerator] / [Denominator]
e.g., Count of Completed Orders / Count of All Orders Placed

Aggregation:
[e.g., COUNT DISTINCT users / COUNT events / SUM(revenue)]

Dimensions:
[List dimensions this metric can be broken down by, as stated or implied]

Filters:
[List all conditions applied. Write "None specified — see Clarification Questions" if absent]

Time Grain:
[e.g., Monthly | Daily | Rolling 30-day | "Not specified — see Clarification Questions"]

Output Type:
[Percentage / Count / Rate / etc.]

Assumptions:
- [Assumption 1]
- [Assumption 2]
- [List every inference made to fill in unstated details]

Clarification Questions:
- [Question 1 — specific, answerable, non-overlapping]
- [Question 2]
- [Question 3]
---
```

---

## Rules

1. **Never assume dimensions, filters, or time periods when not explicitly stated.** Flag them as ambiguous instead.
2. **If multiple valid interpretations exist, list all plausible ones** before settling on the most likely — or ask the user to choose.
3. **Prefer business-readable language** over SQL or technical syntax in the Business Definition and Calculation Logic.
4. **Preserve original intent** — do not reframe the KPI into something different from what was asked.
5. **Assumptions must be explicit** — every inference gets its own bullet.
6. **Clarification questions must be specific and answerable** — not vague ("can you explain more?") but targeted ("Does 'active user' mean logged in within the last 30 days, or any non-deleted account?").
7. **One KPI per output block** — if the user provides multiple KPIs, produce one full block per KPI.

---

## Example

**Input:** "Monthly churn rate by product"

```
---
Original KPI:
Monthly churn rate by product

Business Definition:
The percentage of customers who stopped using a given product during a calendar month, out of those who were active at the start of that month. Used to track retention health and identify products with higher-than-expected customer drop-off.

Calculation Logic:
Customers lost during the month / Customers active at start of month × 100

Aggregation:
COUNT DISTINCT (churned customers) / COUNT DISTINCT (active customers at month start) × 100

Dimensions:
- Product (as stated)
- Month (implied by "monthly")
- Optionally: Customer Segment, Region, Plan Tier (not stated)

Filters:
- Not specified — see Clarification Questions
  (e.g., should trial/free-tier customers be included? What defines "active"?)

Time Grain:
Monthly (calendar month)

Output Type:
Percentage

Assumptions:
- "Churn" means a customer cancelled, did not renew, or became inactive — exact definition unclear
- "Active at start of month" means the customer had a live subscription or usage record on Day 1 of the month
- Customers who churned and reactivated within the same month are counted as churned

Clarification Questions:
- How is "churned" defined — explicit cancellation, non-renewal after contract end, or inactivity for N days?
- Are free-tier or trial customers included in the churn calculation?
- Should customers who downgraded (but not fully cancelled) be counted as churned?
- Is "product" a product line, a specific SKU, or a subscription plan tier?
- Should the denominator include customers who joined mid-month, or only those active at the start?
---
```

---

## Handling Multiple KPIs

If the user provides a list of KPIs, process each one in its own output block with a numbered header:

```
### KPI 1: [Name]
[Full output block]

### KPI 2: [Name]
[Full output block]
```

---

## When to Ask Before Proceeding

If the KPI input is fewer than 5 words and highly ambiguous (e.g., just "sales performance" or "user growth"), ask one targeted clarifying question before producing the full output:

> "Before I structure this — are you measuring total revenue, number of units sold, or something else?"

For anything with reasonable context, attempt a full decomposition first and surface ambiguity in the Clarification Questions section.
