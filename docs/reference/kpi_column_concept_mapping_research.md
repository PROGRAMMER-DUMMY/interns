# KPI / Column-to-Concept Mapping — Research Grounding

Version: 2026-08-03 research pass

Grounding material for redesigning this repo's KPI/column-mapping resolution engine
(`core/onboarding/kpi/feature_resolver.py` + `core/onboarding/relationships/schema_alias_matching.py`)
around a "map to concept, not to column" protocol: profile columns independent of task, define
canonical name-agnostic concepts, score column-to-concept matches on multiple signals (never on
name similarity alone), surface conflicts instead of silently auto-merging, and distinguish a
concept that is genuinely derivable via formula from a merely correlated proxy that must never
silently substitute for it. This pass checks that protocol against four established bodies of
practice: academic schema-matching research, commercial semantic/metrics layers, automated feature
engineering and feature stores, and metric-governance literature. Sources are peer-reviewed papers
(read from their own venue/publisher pages where full text was retrievable), vendor official
documentation, and first-party engineering blogs — never a secondary blog's summary of a summary.
Where full primary text could not be retrieved (several PDFs served binary-encoded streams that
could not be parsed in this session), that is stated explicitly rather than passed off as verified.
Each section closes with a **Cross-reference to this protocol** callout flagging where the protocol
is more rigorous than documented practice, and where documented practice covers ground the protocol
as described does not.

## 1. Schema matching / data integration literature

The foundational taxonomy comes from Erhard Rahm and Philip Bernstein's "A Survey of Approaches to
Automatic Schema Matching" (*The VLDB Journal*, 10(4):334–350, 2001). It classifies matchers along
three axes: **schema-level vs. instance-level** (matching on schema metadata like names/types/
structure vs. matching on the actual data values), **element-level vs. structure-level** (matching
individual attributes vs. matching whole sub-structures), and **language-based (linguistic) vs.
constraint-based** matchers (name/description similarity, possibly via a thesaurus, vs. data types,
value ranges, uniqueness, and relationship cardinalities). Crucially, the survey frames combining
matchers as either **hybrid** (one algorithm considers multiple criteria — e.g. name and type and
structure — simultaneously) or **composite** (several matchers run independently and their outputs
are combined afterward), and only score combinations above a chosen threshold are treated as match
candidates at all. The survey also documents that real systems support 1:1, 1:n, and n:m match
cardinality — i.e., the taxonomy already has a category for "two schema elements are both valid
sources of the same target concept" (1:n) as distinct from a forced 1:1 that turns out wrong; this
is the taxonomy's version of "duplicate sources of the same concept" as a first-class, expected
outcome rather than an error to be silently resolved.

Three named systems operationalize this: **COMA** (Do & Rahm, "COMA — A System for Flexible
Combination of Schema Matching Approaches," VLDB 2002) runs multiple matchers (name similarity, type
similarity, structural similarity) and aggregates their outputs with a configurable function — max,
min, average, or weighted average — before applying a threshold; ambiguous cases (multiple
candidates close in score) are left for a human to resolve rather than auto-picked, and the
numerical similarity score itself is the confidence signal shown to the user. **Cupid** (Madhavan,
Bernstein & Rahm, VLDB 2001) combines linguistic similarity (name and synonym matching, using a
domain thesaurus) with structural similarity into a single weighted coefficient, with a documented
bias toward matching at the leaf level of a schema "where much of the schema content resides" —
i.e., it explicitly does not treat name similarity alone as sufficient and instead requires
structural corroboration. **Similarity Flooding** (Melnik, Garcia-Molina & Rahm, ICDE 2002) starts
from an initial string-based similarity between graph nodes, then propagates ("floods") that
similarity through the schema's structural neighbors to a fixpoint, and finally applies a *filter*
(e.g., a Stable Marriage filter) to select a coherent, low-conflict subset of the propagated
similarity graph as the final mapping. Across this literature, schema matching is consistently
framed as producing *candidate* correspondences for human review, not committed decisions — the
matching literature explicitly treats human adjustment as expected and measures system quality
partly by how many manual adjustments a human still has to make after the automated pass (see the
"Uncertainty in Automated Ontology Matching" source below, itself grounded in this same tradition).
A ten-year retrospective, "Generic Schema Matching, Ten Years Later" (Bernstein, Madhavan & Rahm,
*PVLDB* 4(11):695–701, 2011, VLDB 10-Year Best Paper Award), confirms the field matured into a major
commercial and research topic in the decade after the original survey; its full text could not be
extracted in this session (the PDF served as an unparseable binary stream), so its specific new
findings are not asserted here beyond what dblp/ACM's own listing confirms about the paper's
existence, venue, and award.

**Sources:** [Rahm & Bernstein — A Survey of Approaches to Automatic Schema Matching (VLDB Journal, Springer)](https://link.springer.com/article/10.1007/s007780100057);
[COMA — A System for Flexible Combination of Schema Matching Approaches (dbs.uni-leipzig.de, authors' own institution)](https://dbs.uni-leipzig.de/file/COMA.pdf);
[Cupid — Generic Schema Matching with Cupid (Microsoft Research, official)](https://www.microsoft.com/en-us/research/publication/generic-schema-matching-with-cupid/);
[Similarity Flooding — Stanford InfoLab Publication Server](http://ilpubs.stanford.edu:8090/730/);
[Generic Schema Matching, Ten Years Later — ACM DL record](https://dl.acm.org/doi/10.14778/3402707.3402710);
[Uncertainty in Automated Ontology Matching — arXiv](https://arxiv.org/pdf/2310.11723)

**Confidence:** High on the taxonomy itself and on COMA's aggregation-function mechanics (read
directly from the authors' own hosted PDF). Medium-high on Cupid's and Similarity Flooding's
mechanics — corroborated by the papers' own abstracts/official pages and by consistent secondary
description across independent sources, but this session could not render the full PDF text to
quote exact formulas verbatim (the tool chain here lacks a PDF-to-text renderer). Low on any claim
about the 2011 retrospective beyond its existence/venue/award, since its content could not be
verified at all in this session — do not treat any specific "what changed since 2001" claim as
sourced from that paper.

**Cross-reference to this protocol:** The protocol's "never silently auto-merge on name similarity
alone" and "surface conflicts explicitly" are not new ground — they match established practice
almost exactly (composite/hybrid multi-matcher combination, threshold-gated candidates, n:m
cardinality as an expected outcome, human-adjustable output). Where the literature is *thinner* than
what the protocol proposes: none of COMA/Cupid/Similarity Flooding maintain a persistent,
workspace-level canonical concept dictionary that mappings accumulate evidence against over time —
they are pairwise, schema-to-schema tools; matching against a stable mediated/global schema is
mentioned in the taxonomy but not deeply worked out in any of the three named systems' own papers.
The protocol's persistent "concept" layer is closer to later mediated-schema and ontology-matching
work than to these three systems specifically — a real design choice, not something to assume is
already solved by citing COMA/Cupid/SF.

## 2. Semantic/metrics layers in modern data stacks

dbt's Semantic Layer, built on **MetricFlow**, defines an explicit, name-agnostic **entity**
concept in each semantic model, separate from the physical column: per dbt's own docs, "entities
(join keys)... represent real-world concepts in a business (such as customers or transactions)," and
an entity is declared with a business `name` plus a type (`primary`, `unique`, `foreign`, `natural`)
plus an `expr` that maps the name to whatever expression actually produces it in that table —
including a raw column, a `substring(...)` expression, or anything else. MetricFlow then builds its
join graph from entity **names**, not raw column names, which is precisely how it lets the same
business concept (e.g. `user`) exist as differently-named or differently-derived columns across
different semantic models: each model's entity `expr` handles its own physical reality while the
shared `name` is what MetricFlow actually joins on. Metrics themselves (simple, ratio, cumulative,
derived) are built on top of measures (aggregations over semantic-model columns), one further layer
removed from any physical column name. **Cube.dev** models the same separation as "cubes" (business
entities like customers or orders, each with named dimensions and measures) with **views** sitting
on top as a governed facade — Cube's own docs say views are "useful for defining metrics, managing
governance, and controlling which part of the data model is exposed to end-users," i.e. a
declared abstraction boundary between physical cubes and what a consumer sees. Cube's officially
documented **multi-fact views** feature is a close match to "union with a source tag": when a view
needs measures from more than one fact table that share common dimensions (e.g. orders and returns
sharing customer/date), Cube runs a separate aggregating subquery per fact table and `FULL JOIN`s the
results on the shared dimension columns, rather than joining the fact tables directly (which would
produce a row-multiplying cross product). **Looker's LookML** documents the same union pattern at
the community level (not as a first-class named feature the way Cube's multi-fact-views is): a
SQL-based derived table can union per-source subqueries, and the standard community pattern adds a
source-identifying column before the `UNION ALL`. Looker's own officially documented **Merging
results from different Explores** feature is a separate, weaker mechanism — an in-memory,
non-SQL-join combination of two Explores matched on a shared dimension value, which Looker's own
docs frame as useful when "Looker developers haven't created the relationships you need," i.e.
explicitly a workaround rather than the primary modeling path. LookML itself is built from
dimensions and measures scoped to one view's SQL column plus Explores that join views — it lacks a
first-class object as clean as MetricFlow's named, typed entity; cross-view concept consistency in
LookML relies on naming convention and view reuse, not a declared abstraction object. **AtScale**
markets a "conformed dimension" concept (a dimension with the same meaning reused across multiple
fact sources) and an "Automatic Model Generation" capability said to let a new data source reuse
existing standardized definitions, but the material found for this pass was blog/marketing-level,
not deep technical reference docs on the actual mechanics — flagged as lower confidence below.

**Sources:** [dbt Developer Hub — Entities](https://docs.getdbt.com/docs/build/entities);
[dbt Developer Hub — Semantic models](https://docs.getdbt.com/docs/build/semantic-models);
[dbt Developer Hub — Joins (MetricFlow join logic)](https://docs.getdbt.com/docs/build/join-logic);
[Cube documentation — Concepts](https://cube.dev/docs/product/data-modeling/concepts);
[Cube documentation — Multi-fact views](https://docs.cube.dev/docs/data-modeling/multi-fact-views);
[Looker/Google Cloud — LookML terms and concepts](https://cloud.google.com/looker/docs/lookml-terms-and-concepts);
[Looker/Google Cloud — Merging results from different Explores](https://docs.cloud.google.com/looker/docs/merged-results);
[AtScale — About AtScale Virtual Cubes](https://documentation.atscale.com/installer/creating-and-sharing-cubes/atscale-cube-design-concepts/about-atscale-virtual-cubes)

**Confidence:** High on dbt/MetricFlow entities and Cube's cubes/views/multi-fact-views — read
directly from official reference docs with exact mechanics (entity types, `expr`, the multi-fact
`FULL JOIN`-per-fact-table pattern). High on Looker's official LookML vocabulary and the official
"Merging results" mechanics. Medium on the LookML community UNION pattern — it is a widely-used,
consistently-described pattern but is a community convention, not a named first-class LookML
feature the way Cube's multi-fact-views is. Low-medium on AtScale specifics — only blog-level
material was available in this pass, not AtScale's own deep technical reference for how conformed
dimensions are mechanically resolved across sources.

**Cross-reference to this protocol:** All four tools assume a human has already decided the
column-to-concept mapping and simply encode that decision (in YAML, LookML, or a design canvas) —
none of them document an automatic, multi-signal confidence score for *whether* a given column
really is the named entity/dimension. In that sense the protocol's HIGH/MEDIUM/LOW inference layer
is doing something none of these four tools attempt: they govern an already-resolved mapping, the
protocol is trying to resolve it in the first place. Where these tools are ahead of what the
protocol describes: once a concept is declared, dbt semantic models get versioned, tested, and
put under **contracts** (a real CI-enforced lifecycle), and Looker/Cube both document RBAC and
access governance scoped to the semantic object. The protocol as described stops at "map and score
the concept" — it does not yet describe what happens to a concept mapping's confidence tier over
time (revalidation on schema drift, ownership, change approval), which is exactly the governance
layer these four tools put on top of their entity/cube/dimension abstraction.

## 3. Automated feature engineering / feature store practice

**Featuretools'** official Deep Feature Synthesis docs describe stacking two primitive types —
aggregation primitives (e.g. `count`, `mean`, computed across a one-to-many relationship) and
transform primitives (e.g. `month`, computed within one row) — to synthesize new features, with each
stacking increasing the feature's "depth." Computability is gated purely by type matching: a
primitive declares `input_types` (e.g. `[Numeric, Numeric]`) and can only be applied where matching
columns exist; missing-value handling is per-primitive (e.g. `Mean`'s `skipna` parameter, defaulting
to skipping nulls) rather than a property of the synthesis process as a whole. Nothing in
Featuretools' own docs, across the versions checked, documents a confidence or quality score
attached to a synthesized feature before it is used — computability (do the types line up) is the
only gate. **Feast** separates a `FeatureView` (entities + a data source + features, as they exist
verbatim in a source) from an `OnDemandFeatureView` (features derived from existing features plus
request-time data via user-defined transformation logic) — i.e. Feast's own docs distinguish
"feature as stored" from "feature as derived," but the derivation logic itself is hand-written by
the user, not automatically inferred or confidence-scored. Feast does ship an **alpha** data-quality
integration with Great Expectations: a user prepares a reference dataset, a Great-Expectations-based
profiler generates an `ExpectationSuite` from it, and a subsequently retrieved dataset is validated
against that suite — but Feast's own docs are explicit that "Feast... is not purpose built to solve
data drift / data quality issues," i.e. this is an experimental add-on, not core practice. **Tecton's**
official monitoring docs describe **Data Quality Validation**, which "automatically detects data
quality issues after Feature View materialization" and "sends alert emails when validation results
indicate data fails to meet expectations during a materialization interval" — again, this validates
the *values* a feature produces after the fact (drift/anomaly detection against expectations), not
whether the feature's derivation logic itself should be trusted before first production use. No
source found in this pass — Featuretools, Feast, or Tecton's own docs — describes a pre-production
HIGH/MEDIUM/LOW (or equivalent) confidence tier gating whether a derived feature is trustworthy at
all, comparable to what the protocol proposes.

**Sources:** [Featuretools — Deep Feature Synthesis (official docs)](https://featuretools.alteryx.com/en/stable/getting_started/afe.html);
[Featuretools — Feature primitives (official docs)](https://featuretools.alteryx.com/en/stable/getting_started/primitives.html);
[Feast — Feature view](https://docs.feast.dev/getting-started/concepts/feature-view);
[Feast — On demand feature view (beta)](https://docs.feast.dev/reference/beta-on-demand-feature-view);
[Feast — Data quality monitoring (alpha)](https://docs.feast.dev/reference/dqm);
[Tecton — Monitoring](https://docs.tecton.ai/docs/monitoring)

**Confidence:** High on Featuretools' primitive/type-matching mechanics and the absence of any
documented confidence-gating concept — read directly from official docs across multiple versions,
consistently. High on Feast's FeatureView/OnDemandFeatureView distinction and its own explicit
"not purpose built for data quality" framing. Medium on Tecton — the top-level monitoring page
confirms Data Quality Validation exists and what it broadly does, but this session could not fetch
the deeper `/docs/monitoring/data-quality-validation` sub-page to confirm exact threshold/blocking
behavior (does a failed validation block a feature from serving, or only alert) — that specific
claim is not independently verified here.

**Cross-reference to this protocol:** This is the section where the protocol is clearly ahead of
documented practice, not just aligned with it. Feature-engineering/feature-store tooling gates on
"is this computable" (types line up) and, at best, "does the computed value look statistically
normal after the fact" (drift/expectations) — none of it gates on "is this the *right* feature to
trust in the first place," which is exactly the protocol's HIGH/MEDIUM/LOW pre-production tier and
its derivable-vs-proxy distinction. There is no established template to point to for this part of
the protocol's design — it should be built and defended on its own merits, not justified by
"Feast/Tecton already do this," because they do not.

## 4. Metric governance / avoiding proxy substitution

Kimball's dimensional-modeling discipline grounds the general principle of a governed, shared
"single version of truth" object: **conformed dimensions** are dimension tables that "conform when
attributes in separate dimension tables have the same column names and domain contents," built and
maintained as centralized master data and reused across fact tables specifically so that "there's no
reason to replicate" the same measures/attributes redundantly — and Kimball's own material is
explicit that this requires "a commitment and investment in data stewardship and governance,"
defined in collaboration with business stakeholders, not unilaterally by an engineering team. This
grounds the *process* discipline (shared definitions require governance, not ad hoc engineering
judgment) but Kimball's conformed-dimension material is about dimensional attributes, not
specifically about detecting when a correlated proxy measure has been substituted for a true source
measure — no Kimball-specific source found in this pass addresses that exact scenario head-on.
dbt Labs' own guidance for semantic-layer governance (a company blog post, not a peer-reviewed
source) recommends "establishing clear ownership for each metric," "creating an approval process for
metric changes," and "setting up production testing and data health signals to issue alerts on
metric quality issues" — again, process/change-management discipline around an already-defined
metric, not a documented method for distinguishing a derivable concept from a proxy at definition
time. The clearest documented cautionary tale for proxy-metric governance failure found in this pass
is **Wells Fargo's cross-sell scandal**: the bank's own internal KPI ("cross-sell ratio," averaging
household units of Wells Fargo product) was used as a management target and proxy for genuine
customer-relationship growth; per contemporaneous reporting, employees under pressure to hit
"50/50 plans" opened millions of accounts without customer consent to move the number, the fake
accounts generated negligible real business value, and Wells Fargo ultimately eliminated the metric
entirely ("the cross-sell metric will not be included going forward," per its own 2017
announcement). This is honestly closer to a *gameable-KPI-as-target* governance failure
(Goodhart's Law in practice: "when a measure becomes a target, it ceases to be a good measure," per
Charles Goodhart, 1975) than a literal "column A silently substituted for column B in a data
pipeline" case — it is the best-documented real-world cautionary example found, but it is not a
one-to-one match for the protocol's specific "contracted rate vs. average paid amount" scenario, and
that gap should not be papered over. The closer technical match is Spotify's own experimentation
engineering blog, "When Proxy Metrics Break: How Optimizing for Proxies Can Backfire," which
documents a team boosting a proxy metric ("Liked Songs," +20%) whose true north-star outcome
"barely moved" once long-term data arrived, and states the guardrails directly: build for genuine
value rather than gaming the proxy, use multiple correlated proxies rather than one, validate a
proxy against the true outcome via longer-run experiments/holdouts before trusting it, and keep the
model connecting proxy to outcome transparent to the team — i.e. never let a proxy fully and
silently substitute for the true outcome measure, which is precisely the protocol's own rule stated
for a different domain (KPI feature resolution rather than product experimentation).

**Sources:** [Kimball Group — Conformed Dimensions](https://www.kimballgroup.com/data-warehouse-business-intelligence-resources/kimball-techniques/dimensional-modeling-techniques/conformed-dimension/);
[dbt Labs — Centrally defined metrics: The key to AI success](https://www.getdbt.com/blog/centrally-defined-metrics);
[CNN — Wells Fargo dumps toxic 'cross-selling' metric](https://money.cnn.com/2017/01/13/investing/wells-fargo-cross-selling-fake-accounts/index.html);
[Spotify Confidence — When Proxy Metrics Break](https://confidence.spotify.com/blog/proxy-metrics)

**Confidence:** High on Kimball's conformed-dimension governance discipline (read directly from
Kimball Group's own published technique page) and on the Wells Fargo case (multiple independent,
credible contemporaneous sources, including CNN's direct reporting of Wells Fargo's own statement
retiring the metric). High on the Spotify post's specific example and guardrails — a first-party
engineering/experimentation-platform blog, directly on point. Medium on dbt Labs' governance
guidance — it is the vendor's own blog, useful as documented practitioner guidance, but promotional
in framing and not peer-reviewed or independently corroborated. Low/anecdotal on "Kimball explicitly
addresses proxy-vs-true-metric substitution" — no source found in this pass makes that connection
directly; treat the Kimball material as adjacent grounding (governed shared definitions), not as a
documented proxy-substitution warning.

**Cross-reference to this protocol:** The protocol's specific rule — a concept is either genuinely
derivable via formula over resolved concepts, or it is BLOCKED, and a correlated/computable proxy
must never silently stand in for it — is a sharper, more specific, and more mechanically enforced
version of what Goodhart's Law and Spotify's own post warn about only in the abstract ("don't let a
measure become a target," "never substitute entirely"). No source found in this pass documents a
formal, pre-production, required-sign-off gate at the exact moment a missing KPI concept is about to
be filled by a correlated proxy — established governance practice (Kimball, dbt, Looker) governs
metrics *after* they are defined and in production (ownership, change approval, drift alerts), not
at the point of initial concept resolution. That means this part of the protocol has no existing
industry template to crib from — it is filling a real, documented-as-a-failure-mode-but-not-
documented-as-solved gap, and should be designed and tested on its own terms rather than assumed
safe because "Kimball/dbt already solved this."
