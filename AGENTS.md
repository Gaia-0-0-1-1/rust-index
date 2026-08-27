# Using this repo as an index

`README.md` is a curated list of ~1,700 Rust projects. This directory turns it
into something an agent can query instead of scan.

**`README.md` is the only source of truth. Everything in `index/` is generated —
regenerate it, never hand-edit it.**

## Query it
`AGENT-PROMPT.md` is a self-contained version of this page, meant to be pasted
to an agent that does not have the repo yet.


```bash
python tools/rustgraph.py ask "async http client"
```

Also: `cat "<category path>"` (a category's members are mutual alternatives),
`node <name|crate>` (everything known about one project), `dag` (dependency
strata and which projects are most load-bearing), `recipe [name]`, `stats`.

```bash
python tools/rustgraph.py recipe cli
```

Recipes are stacks per archetype — cli, web-service, tui, parser, data,
http-client, wasm — in `tools/recipes.json`. They are the one **hand-authored**
thing here, so every crate they name is resolved against the index on each run: a
name that is stale, mistyped or invented prints `NOT IN THIS LIST` rather than
passing as advice. Each step leads with the *role* ("Argument parsing") and the
crate second, because the role is the durable half.

That resolution doubles as a gap report on the list itself. `recipe cli` marks
**anyhow** uncovered and `recipe parser` marks **serde** — neither is in
`README.md`, though real stacks need both.

Every line of output cites `README.md:<line>`, so a claim can always be checked
against the source.

**How `ask` ranks.** The taxonomy is matched first, because in a hand-curated list
it carries more signal than the prose — matching names alone lets "async" drag in
async-std, async-dropper and async-graphql on nothing but string overlap. Beyond
the category names, each category also carries the vocabulary its members are
*described* with, aggregated over its whole subtree; that is what lets
"serialization" reach Encoding, whose name never says the word. Adoption then
orders the results, but only *within* the categories that matched: counted
globally, clap's billion downloads win "terminal ui" on fame alone.

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

## The three gates

**Accounting — catches parsing drift.** `extract` enforces, and exits non-zero on:

```
entries + labels == every "* " bullet in README.md outside the TOC
                    (1718 + 240 == 1958)
```

Every bullet must be exactly one of the two. Fenced code blocks are skipped, so a
`* ` in a sample never counts. A mismatch means parsing has drifted, and the run
fails rather than writing a plausible-looking wrong file.

**Staleness — catches an index older than the README.** The `lint-index` job in
`.github/workflows/lint.yml` regenerates the README-derived artifacts and fails on
any diff, the same shape as the existing `lint-markdown-toc` job.

> **If `lint-index` fails after a README change** — which is expected after
> merging upstream — the index is simply out of date. Run `cargo run --bin index`
> and commit the resulting `index/` diff.

**Answers — catches the query surface regressing.** `tools/selftest.json` records
what "the index works" means: nine queries with their expected top crate, one that
must find no category at all, and each recipe's resolved/uncovered counts.

```bash
python tools/rustgraph.py selftest
```

It is also the only Python CI runs, so without it a syntax error in
`rustgraph.py` or a rotted recipe would sail through green.

> **If `selftest` fails**, decide which happened. A ranking change you did not
> intend is a bug. A README edit or a fresher crates.io dump legitimately moving
> an answer is not — update `tools/selftest.json` in the same commit and say why.

CI rebuilds only the README-derived artifacts. The crates.io ones need a 1.8GB
dump, so they are refreshed deliberately by running `tools/crates_deps.py`, not on
every push.

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
5. **A query still has to land on some category.** Category names are matched
    first, then the vocabulary its members are *described* with, which covers most
    wording the list itself never uses. When nothing clears the bar `ask` prints
    `NO CATEGORY MATCHED THIS WORDING` and marks the results as guesses; browse
    with `cat` and retry in the list's own terms.
6. **Dump-derived artifacts are a snapshot.** Check `crates-provenance.json` and
    re-run `crates_deps.py` if the download counts look stale.
