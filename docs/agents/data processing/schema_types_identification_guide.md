# Complete Guide to Database Schema Types
### How to Identify, Distinguish, and Use Every Major Schema

> Canonical schema-identification reference for the data-understanding gate (see BUG-010).
> Used to classify a workspace's schema type (star / snowflake / galaxy / flat / 3NF / OBT /
> hierarchical / graph / document / etc.) from the data model + profiles before onboarding.

---

## Table of Contents

1. What Is a Database Schema?
2. Star Schema
3. Snowflake Schema
4. Galaxy Schema (Fact Constellation)
5. Flat Schema
6. Hierarchical Schema
7. Network Schema
8. Relational Schema
9. Entity-Relationship (ER) Schema
10. Object-Oriented Schema
11. Document Schema (NoSQL)
12. Key-Value Schema (NoSQL)
13. Column-Family Schema (NoSQL)
14. Graph Schema
15. Third Normal Form (3NF) Schema
16. Data Vault Schema
17. Anchor Schema
18. One Big Table (OBT) Schema
19. Quick Identification Reference Table
20. How to Distinguish Schemas: Decision Framework
21. Key Signals Cheat Sheet

---

## 1. What Is a Database Schema?

A **database schema** is the logical blueprint that defines:
- How data is **organized** (tables, documents, nodes, etc.)
- How data **relates** to other data (foreign keys, edges, references)
- What **constraints** apply (primary keys, nullability, data types)

Schemas are chosen based on:
- **Use case** — OLTP vs. OLAP vs. real-time
- **Query patterns** — simple lookups vs. complex analytics
- **Data shape** — structured, semi-structured, unstructured
- **Scale** — millions vs. billions of records
- **Flexibility** — fixed vs. evolving structure

---

## 2. Star Schema

A **Star Schema** has one central **Fact Table** surrounded by multiple **Dimension Tables**.

```
         [Date Dimension]
               |
[Product Dim]--[FACT TABLE]--[Customer Dim]
               |
         [Store Dimension]
```

| Component | Role | Example |
|-----------|------|---------|
| Fact Table | Stores measurable events/metrics | Sales transactions |
| Dimension Table | Stores descriptive context | Customer info, Date, Product |
| Foreign Keys | Link dimensions to the fact | customer_id, product_id |
| Grain | One row = one specific event | One sale per row |

How to identify:
- One large central table with many numeric columns (facts/measures)
- Multiple surrounding smaller tables with descriptive columns
- Central table has multiple foreign keys pointing outward
- Dimension tables do NOT reference each other
- Table names often contain `fact_` / `dim_`

Best for: data warehouses, BI dashboards, read-query performance.

---

## 3. Snowflake Schema

A **Snowflake Schema** is a normalized Star where dimension tables split into sub-dimensions.

```
[Sub-Category]-[Category]-[Product Dim]--[FACT TABLE]--[Customer Dim]-[City]-[Country]
```

| Feature | Star | Snowflake |
|---------|------|-----------|
| Dimension normalization | Denormalized (flat) | Normalized (split) |
| Number of joins | Few (1 level) | More (2-3+ levels) |
| Storage | More (redundancy) | Less |
| Query complexity | Simple | More complex |

How to identify:
- Dimension tables reference OTHER dimension tables (not just the fact)
- Chains like `dim_city` -> `dim_country` -> `dim_region`
- Foreign keys WITHIN dimension tables pointing to other dimensions

Best for: storage efficiency, hierarchical dimensions (geography, time), consistency-critical systems.

---

## 4. Galaxy Schema (Fact Constellation)

**Multiple fact tables** sharing common dimension tables.

```
[Date Dim]       [Customer Dim]
    |    \       /     |
[FACT: Sales]  [FACT: Returns]
    |                  |
[Product Dim]    [Store Dim]
```

How to identify: more than one fact table; dimensions shared between facts; looks like multiple
stars connected at dimensions. Best for: enterprise DWs spanning multiple business processes.

---

## 5. Flat Schema

All data in a **single table**, no relationships.

How to identify: one table (or few, no joins); heavy repetition; no foreign keys; CSV/spreadsheet
exports. Best for: small datasets, one-off exports, read-only prototypes.

---

## 6. Hierarchical Schema

Tree structure — each child has exactly one parent.

How to identify: `parent_id` self-referencing foreign key; org charts, file systems, category
trees, XML/JSON. Best for: organizational hierarchies, directory structures.

```sql
CREATE TABLE employee (
  emp_id INT PRIMARY KEY,
  name VARCHAR(100),
  manager_id INT REFERENCES employee(emp_id) -- self-reference
);
```

---

## 7. Network Schema

Hierarchical + **many-to-many** (a child can have multiple parents). Legacy (CODASYL). Uses
pointer/link structures; resembles a graph with record-set ownership. Rare in modern systems.

---

## 8. Relational Schema

Standard for modern transactional DBs — normalized tables linked by foreign keys.

How to identify: multiple tables with clear primary keys; FK constraints enforcing referential
integrity; normalized (1NF-3NF); ACID; standard SQL.

| Form | Rule |
|------|------|
| 1NF | No repeating groups; atomic values |
| 2NF | 1NF + no partial dependencies on composite keys |
| 3NF | 2NF + no transitive dependencies |
| BCNF | Every determinant is a candidate key |
| 4NF | No multi-valued dependencies |
| 5NF | No join dependencies |

Best for: OLTP (e-commerce, banking, ERP); integrity-paramount systems.

---

## 9. Entity-Relationship (ER) Schema

A **conceptual design model** (diagram, not physical) using entities, attributes, relationships.

| Symbol | Meaning |
|--------|---------|
| Rectangle | Entity (table) |
| Ellipse | Attribute (column) |
| Diamond | Relationship |
| Double rectangle | Weak entity |

Cardinality: 1:1 (Person-Passport), 1:N (Customer-Orders), M:N (Students-Courses).

---

## 10. Object-Oriented Schema

Maps DB structures to OOP — classes, inheritance, encapsulation.

| OOP Concept | Schema Equivalent |
|-------------|-------------------|
| Class | Table/Type |
| Object | Row/Instance |
| Inheritance | Table hierarchy |
| Method | Stored procedure on type |

How to identify: user-defined types (UDTs), inheritance hierarchies, complex/nested column types.
Examples: PostgreSQL (extensions), Oracle object types.

---

## 11. Document Schema (NoSQL)

Self-contained JSON/BSON/XML documents; each can differ in structure.

```json
{ "_id": "order_001", "customer": {"name": "Alice"}, "items": [{"product": "Widget A", "qty": 2}], "total": 24.97 }
```

How to identify: JSON/BSON; collections (not tables), documents (not rows); no uniform structure;
native nested objects/arrays. Examples: MongoDB, CouchDB, Firestore.

---

## 12. Key-Value Schema (NoSQL)

`key -> value` pairs, no enforced internal structure.

How to identify: each record is key + opaque value; query only by key; extremely fast lookups.
Examples: Redis, DynamoDB (basic), Memcached. Best for: caching, sessions, carts, leaderboards.

---

## 13. Column-Family Schema (NoSQL)

Rows where each can have a different set of columns, grouped into column families.

How to identify: column families (not just columns); sparse/variable columns; massive horizontal
scale. Examples: Cassandra, HBase, Bigtable. Best for: time-series, IoT, write-heavy at scale.

---

## 14. Graph Schema

Nodes (entities) + edges (relationships), optimized for traversing connected data.

```
(Alice)-[:FRIEND]->(Bob)-[:WORKS_AT]->(Acme Corp)
```

How to identify: nodes and edges (not tables); relationships are first-class with properties;
traversal query language (not JOINs). Examples: Neo4j (Cypher), Neptune, ArangoDB. Best for:
social networks, fraud detection, recommendations, knowledge graphs.

---

## 15. Third Normal Form (3NF) Schema

A relational schema designed to eliminate redundancy/anomalies through strict normalization.
Standard for OLTP.

Rules: 1NF (atomic) + 2NF (no partial deps) + no transitive dependencies (non-key columns depend
ONLY on the primary key).

How to identify: no non-key column depends on another non-key column; many lookup tables
(city, country, status, type); frequent small tables with simple joins.

---

## 16. Data Vault Schema

Enterprise DW methodology separating structure (Hubs), context (Satellites), relationships (Links).

| Component | Role | Example |
|-----------|------|---------|
| Hub | Unique business keys | Hub_Customer(customer_id) |
| Satellite | Attributes/context with history | Sat_Customer_Details(name, load_date) |
| Link | Relationships between hubs | Link_CustomerOrder(customer_key, order_key) |

How to identify: `hub_` / `sat_` / `lnk_` prefixes; `load_date` + `record_source` on every table;
no direct hub-hub joins (always via links); insert-only history. Best for: auditable enterprise
DWs, regulated industries.

---

## 17. Anchor Schema

Extreme normalization — every attribute gets its own table, all temporal.

How to identify: one table per attribute; temporal history everywhere; hundreds/thousands of
tables; complex joins but zero-ALTER schema evolution. Best for: constantly-changing models with
full historical tracking.

---

## 18. One Big Table (OBT) Schema

Fully denormalized — all data joined into a single wide table, for analytical engines.

How to identify: one table, 50-500+ columns; very wide rows; no joins; high redundancy; columnar
storage (Parquet, BigQuery, ClickHouse). Best for: cloud analytics, dbt-modeled analytics layers.

---

## 19. Quick Identification Reference Table

| Schema | # Tables | Joins Needed | Main Use | Key Identifier |
|--------|----------|--------------|----------|----------------|
| Star | Medium | Few (1 level) | OLAP / BI | Central fact + surrounding dims |
| Snowflake | Many | More (2-3) | OLAP / BI | Normalized dimension chains |
| Galaxy | Many | Many | Enterprise DW | Multiple facts sharing dims |
| Flat | 1 | None | Exports, CSV | Single table, repeated data |
| Hierarchical | 1-few | Self-joins | Trees, org charts | Parent-child self-reference |
| Network | Multiple | Complex | Legacy | Many-to-many pointer links |
| Relational (3NF) | Many | Many | OLTP | FK constraints, normalized |
| ER Model | N/A (diagram) | N/A | Design phase | Conceptual boxes-and-lines |
| Object-Oriented | Medium | Few | OOP-heavy apps | UDTs, inheritance |
| Document | Collections | Embedded | CMS, profiles | JSON docs, nested arrays |
| Key-Value | Buckets | None | Cache, sessions | Key -> opaque value |
| Column-Family | Column families | None | IoT, logs | Sparse variable columns |
| Graph | Nodes/Edges | Traversal | Social, fraud | Nodes + named edge types |
| Data Vault | Many | Via links | Regulated DW | hub_, sat_, lnk_ prefixes |
| Anchor | Extreme many | Many | Volatile schemas | One table per attribute |
| OBT | 1 | None | Cloud analytics | 50-500+ column wide table |

---

## 20. How to Distinguish Schemas: Decision Framework

Step 1 — system type:
- Transaction system (frequent inserts/updates) -> Relational / 3NF
- Analytics/reporting -> Step 2
- Otherwise -> Step 3

Step 2 — analytical schema:
- One fact table + normalized dims -> Snowflake; + denormalized dims -> Star
- Many fact tables -> Galaxy
- Auditability/history of every change -> Data Vault
- All joins pre-done in one table -> OBT

Step 3 — data shape:
- Structured rows/columns -> relational types above
- Semi-structured JSON/XML -> Document
- Key-value pairs -> Key-Value
- Highly connected -> Graph
- Wide sparse rows -> Column-Family
- Tree/hierarchy -> Hierarchical

---

## 21. Key Signals Cheat Sheet

Visual signals:
| If you see... | Probably... |
|---------------|-------------|
| One large table with `_id` columns pointing out | Star |
| Dimension tables with FK to another dimension | Snowflake |
| 2+ fact tables sharing dimensions | Galaxy |
| `hub_` / `sat_` / `lnk_` prefixes | Data Vault |
| Self-referencing FK (`manager_id -> emp_id`) | Hierarchical |
| One single enormous table | Flat or OBT |
| `load_date` / `record_source` on every table | Data Vault |
| Collections of JSON documents | Document |
| Nodes and edges | Graph |

Naming conventions:
| Pattern | Schema |
|---------|--------|
| `fact_sales`, `dim_customer` | Star / Snowflake |
| `hub_customer`, `sat_customer_details` | Data Vault |
| plain nouns: `orders`, `customers` | Relational / 3NF |
| attribute-as-table: `Customer_Name` | Anchor |
| single file `all_sales.csv` | Flat |

Query patterns:
| Query looks like... | Schema |
|---------------------|--------|
| `SELECT ... FROM fact JOIN dim1 JOIN dim2` | Star / Snowflake |
| `MATCH (n)-[:REL]->(m)` | Graph (Cypher) |
| `db.collection.find({})` | Document |
| `GET key` / `SET key value` | Key-Value |
| `JOIN dim JOIN sub_dim JOIN sub_sub_dim` | Snowflake |
| No JOINs, one huge SELECT | Flat / OBT |

---

*End of guide — schema types, identification strategies, and decision frameworks.*
