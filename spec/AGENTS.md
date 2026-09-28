# spec/

One directory per decision: `spec/{YYYY-MM-DD}-{kebab-name}/README.md`. The date is the day the
spec was opened. Supporting material (sketches, benchmarks) sits next to the README.

## Sections

1. **Status** — `draft`, `accepted`, or `frozen (shipped in <commit or version>)`.
2. **Request** — what was asked, in the asker's terms.
3. **Decisions** — what we chose, one subsection each, with the reason.
4. **Rejected** — alternatives we considered and why they lost.
5. **Open questions** — numbered, so discussion can refer to them. Empty once accepted.
6. **Plan** — phases and their exit criteria.

## Lifecycle

A spec is edited freely while `draft`. Once `accepted`, changes go in as dated notes under the
section they touch. When the work ships, mark it `frozen`, move how-it-works content into `docs/`,
and stop editing. A later change that reverses a frozen decision gets its own spec and links back.
