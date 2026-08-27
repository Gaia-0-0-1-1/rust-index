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
`node <name|crate>` (everything known about one project), `dag` (dependency
strata and which projects are most load-bearing), `stats`.

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

Tracked: everything an agent needs on a fresh clone, with no network access and
no build — the README-derived artifacts (`entries.jsonl`, `nodes.jsonl`,
`edges.jsonl`, `categories.json`) and the crates.io-derived ones
(`crate-identity.json`, `downloads.json`, `dep-edges.jsonl`, `dag.json`,
`crates-provenance.json`). About 2MB in total.

Ignored: `entries.enriched.jsonl` and `coverage.json`, which reflect local cache
state and differ per run.

Every tracked artifact regenerates byte-identically **from the same README and
the same dump snapshot**, so a re-run leaves a clean tree. A newer dump will
legitimately dirty it — that is what `crates-provenance.json` is for. They are
pinned to LF in `.gitattributes`; without that, autocrlf rewrites them on
checkout and every regeneration shows a whole-file diff.

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

## Dependency data

```bash
python tools/crates_deps.py <db-dump.tar.gz>   # then: rustgraph.py dag --write
```

`README.md` cannot supply crate identity, adoption, or dependencies. The
[crates.io database dump](https://static.crates.io/db-dump.tar.gz) can. It is
~1.8GB compressed and ~20GB unpacked, so it is streamed twice out of the tarball
and filtered to our nodes — nothing is ever unpacked to disk.

This raised crate identity from 536 to **1,264 of 1,702** projects and produced
**6,974** `depends_on` edges. `index/crates-provenance.json` records which dump
snapshot the committed artifacts came from.

Identity is resolved by descending confidence, because one repo usually publishes
many crates (`tokio-rs/tokio` also ships tokio-util, tokio-stream, …):

| Source | Count | Confidence |
|---|---:|---|
| `readme` — crates.io link in the list | 534 | high |
| `repo-exact` — crate named after the repo | 492 | high |
| `repo-alias` — conventional alias (`curl-rust` → `curl`) | 92 | high |
| `repo-downloads` — no name match, took the most downloaded | 146 | **low** |

`ask` marks the low-confidence 146 with `?` and `UNVERIFIED`.

`rustgraph.py dag` uses **normal dependencies only**. Dev-dependencies are not
runtime edges and they close cycles — serde dev-depends on serde_json, which
depends on serde — which fuses the graph into one component and makes every
transitive-dependent count saturate at the same value. Pass `--with-dev` to see
that for yourself.

## Known limits — read before trusting output

1. **1,264 of 1,702 projects have crate identity; 438 do not.** Those are
    applications, non-Rust resources, and projects not published to crates.io.
    They still rank and cite correctly, they just have no deps or downloads.
2. **146 crate names are low-confidence guesses**, marked `?` / `UNVERIFIED`.
    Confirm on crates.io before running `cargo add` on one.
3. **The DAG covers 1,086 projects**, being those with both crate identity and at
    least one dependency edge to another indexed project. Dependencies on crates
    outside this list are not edges here.
4. **Multi-concept queries surface the dominant concept only.** "async http client
    with rustls" returns HTTP clients; `rustls` needs its own query.
5. **Queries must use the list's vocabulary.** The taxonomy anchors the ranking,
    so a word it does not use finds no category and the results degrade to lexical
    guesses — the list says "Encoding", never "serialization". `ask` prints
    `NO CATEGORY MATCHED THIS WORDING` when this happens; browse with `cat` and
    retry. Compare `ask "serialization"` with `ask "encoding json"`, which returns
    serde_json at 1.2B downloads.
6. **Dump-derived artifacts are a snapshot.** Check `crates-provenance.json` and
    re-run `crates_deps.py` if the download counts look stale.
