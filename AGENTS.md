# Using this repo as an index

`README.md` is a curated list of ~1,700 Rust projects. This directory turns it
into something an agent can query instead of scan.

**`README.md` is the only source of truth. Everything in `index/` is generated —
regenerate it, never hand-edit it.**

## Query it

```bash
python tools/rustgraph.py ask "async http client"
```

Also: `cat "<category path>"` (a category's members are mutual alternatives),
`node <name|crate>` (everything known about one project), `stats`.

Every line of output cites `README.md:<line>`, so a claim can always be checked
against the source.

## Regenerate it

```bash
cargo run --bin index
```

Three stages, each printing its own numbers:

| Stage | Reads | Writes |
|---|---|---|
| `extract` | `README.md` | `index/entries.jsonl` |
| `enrich` | `results/popularity.yaml` | `index/entries.enriched.jsonl`, `index/coverage.json` |
| `graph` | entries | `index/nodes.jsonl`, `index/edges.jsonl`, `index/categories.json` |

Run one stage alone with `cargo run --bin index -- extract|enrich|graph`.

Indexing issues **no network traffic of its own** — `enrich` reuses the cache the
link checker (`src/main.rs`) already fills, joining on the same keys it writes:
the normalised `https://github.com/{org}/{repo}` for stars, the raw crates.io URL
for downloads.

## What's committed

Tracked: artifacts reproducible from `README.md` alone — `entries.jsonl`,
`nodes.jsonl`, `edges.jsonl`, `categories.json`. Ignored: artifacts reflecting
network state — `entries.enriched.jsonl`, `coverage.json`.

## The gate

`extract` prints an exact accounting invariant:

```
entries + labels == every "* " bullet in README.md   (1718 + 240 == 1958)
```

Every bullet is classified as exactly one of the two. If that sum stops matching,
parsing has drifted and the index should not be trusted.

## Model

**Nodes** are projects, one per URL. A project listed under several categories is
*one* node carrying every `category_path` — that span is signal (`wtx` is a
Postgres client *and* an HTTP client *and* a WebSocket library), not duplication.

**Categories** come from the README's own heading and bullet-group structure.
Membership of a category *is* the "these are alternatives" relation, so it is
stored as a member list rather than N² edges.

A bullet is a **category label**, not a project, when it has no ` - ` description —
`* [Vulkan](https://www.vulkan.org/)` and `* AWS [[aws](.../keywords/aws)]` name
technologies to group under. A bullet with a description is a project even when it
also has children (`* [Cargo](https://crates.io/) - the Rust package manager`).

## Known limits — read before trusting output

1. **Ranking is popularity-blind unless `results/popularity.yaml` exists.** That
   file is gitignored and filled by the link checker. Without it, `ask` picks the
   right *category* but the order within it is unreliable — an obscure project can
   outrank the ecosystem standard. Treat the returned set as candidates, not a
   ranking.
2. **Only 536 of 1,702 projects have a crates.io link in the README.** For the
   rest `ask` guesses the crate name from the repo and marks it `?` /
   `UNVERIFIED`. Never run an unverified `cargo add` without checking crates.io —
   the guess is wrong whenever repo and crate names differ (`curl-rust` → `curl`).
   Notably `tokio` has no crates.io link here.
3. **There are no dependency edges yet.** `edges.jsonl` holds category membership
   plus 7 cross-references. The real dependency DAG needs crates.io data that is
   not in this repo.
4. **Multi-concept queries surface the dominant concept only.** "async http client
   with rustls" returns HTTP clients; `rustls` needs its own query.
