# Enterprise — Index

Home for **enterprise-specific platform requirements**: custom asks, contractual constraints,
tenancy/compliance decisions, and bespoke specs that shape how WE build the product for an
enterprise customer. These are platform-internal specs/plans — NOT a customer's generated workspace
output (that stays in `workspaces/<project>/interns/`).

Use this folder when an enterprise wants something custom that isn't a generic plan: a compliance
requirement (e.g. HIPAA/PHI handling), a tenancy model, an auth/RBAC spec, a custom KPI/processing
playbook, or an integration constraint.

| File | What it covers |
|------|----------------|
| _(none yet)_ | Add the first enterprise spec here. |

Related (general roadmaps, not enterprise-custom): `docs/plans/PRODUCTION_ROADMAP.md` carries the
enterprise gap analysis (PHI, tenancy, auth/RBAC) at a roadmap level; when a requirement becomes a
concrete custom spec, capture it here and link back to the roadmap item.

When adding a file: tag it `[plan]` (a spec/requirement) in `docs/README.md`'s enterprise line if it
is forward-looking, add one row above, and link the originating roadmap/customer context.
