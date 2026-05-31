# Data Processing Knowledge Set — Index

Canonical data-engineering + schema reference for this project. Grounds the
**data-understanding gate** (see `docs/bugs/BUG_SESSION_REPORT.md` BUG-010): classifying a
workspace's **data-quality tier** (raw/bronze/silver/gold) and **schema type**
(star/snowflake/galaxy/3NF/OBT/…) from the data model + profiles, before onboarding commits a path.

| File | Covers | Use for |
|------|--------|---------|
| `data_engineering_guide_part1.md` | Mental model; batch / streaming / micro-batch / event-driven processing; data cleaning | Processing-method selection; cleaning patterns |
| `data_engineering_guide_part2.md` | ETL/ELT/ETLT; OLTP vs OLAP; warehouse/lake/lakehouse; **medallion bronze/silver/gold** | **Quality-tier classification**; pipeline architecture |
| `data_engineering_guide_part3.md` | Spark vs Flink; orchestration; modeling; quality; optimization | Engine/orchestration choices; optimization |
| `data_engineering_guide_part4.md` | Data contracts; feature stores; RAG; agentic workflows; anti-patterns; checklist | Emerging methods; avoiding anti-patterns |
| `schema_types_identification_guide.md` | Star/snowflake/galaxy/flat/3NF/OBT/hierarchical/graph/document/… + decision framework + cheat sheet | **Schema-type classification** |

Quick routing:
- "What quality tier is this data?" -> part2 (medallion) + part1 (cleaning signals).
- "What schema type is this?" -> `schema_types_identification_guide.md` decision framework.
- "Which processing/engine method?" -> part1 (methods) + part3 (engines).

When adding a file here: add one row above, then confirm `docs/README.md` still points at this
folder (it points at the folder, not individual files — keep it that way).
