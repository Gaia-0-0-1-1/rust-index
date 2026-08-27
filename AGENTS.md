# Using this repo as an index

`README.md` is a curated list of ~1,700 Rust projects. This directory turns it
into something an agent can query instead of scan.

**`README.md` is the only source of truth. Everything in `index/` is generated —
regenerate it, never hand-edit it.**

`AGENT-PROMPT.md` says all of this in one self-contained page, for pasting to
an agent that does not have the repo yet.

## Query it

```bash
python tools/rustgraph.py ask "async http client"
```

Also: `cat "<category path>"` (a category's members are mutual alternatives),
`node <name|crate>` (everything known about one project), `dag` (dependency
strata and which projects are most load-bearing), `recipe [name]`,
`stack <crate>`, `features <crate> [what]`, `example <crate>`, `stats`.

```bash
python tools/rustgraph.py stack axum
```

`stack` answers the half of the question a category cannot: not "which crate",
but "what comes with it", plus what the crate **requires** versus what is
feature-gated. It is derived from the dependency graph rather than authored — two crates that share a Cargo.toml co-occur, and **lift** corrects for
ubiquity, so it reports `tonic`+`prost` and `quinn`+`rustls` rather than
`everything`+`serde_json`. `ask` shows the same for its leading answer.

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
"serialization" reach Encoding, whose name never says the word. It carries their
**crates.io keywords and categories** too — author-assigned synonyms and a second
taxonomy — which is what lets "REST API" reach HTTP Server, "orm" reach
Database / ORM, and "parallelism" reach Concurrency. None of those words appear
in `README.md`. Adoption then orders the results, but only *within* the
categories that matched: counted globally, clap's billion downloads win
"terminal ui" on fame alone. Adoption *orders* results without deciding them:
name, keyword and description matching still reorder within a category, which is
why reqwest sits above hyper despite having fewer downloads.

**Confidence.** Every `ask` prints `CONFIDENCE high|LOW` with the share of the
query — measured in characters, so a non-ASCII query cannot claim full coverage
off one surviving word — that the index actually matched.

**It is weakly calibrated, and that is measured, not estimated.** On a 30-query
external evaluation, **about a quarter of `high` verdicts were wrong** and **about
half of `LOW` verdicts carried the right answer**. Treat `high` as "the index
matched most of what you asked", not as "this answer is correct", and treat `LOW`
as "check this", not as "this is wrong". A guessed (`?` / UNVERIFIED) crate name
now forces LOW regardless of coverage.

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
`crate-vocabulary.json`, `crates-provenance.json`). About 2MB in total.

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
what "the index works" means: 48 checks across nineteen queries with their
expected top crate, six confidence verdicts, one query that must find no category
at all, each recipe's resolved/uncovered counts, three graph affinities, two
runtime constraints, five feature lookups and three example lookups.

**Runtime constraints.** `ask` and `stack` report when a crate pins an async
runtime, three ways, because Cargo's `optional` flag alone misleads: axum gates
tokio behind a feature that `default` turns on, so calling it merely optional
tells you that you have a choice you do not have. Only a dependency that is
optional *and* outside the default-feature closure is genuinely opt-in — `lettre`
is, `axum` is not. Worth knowing: **no crate in this index requires both tokio
and async-std**, so there is no conflict detector here; it would never fire.

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

## Cargo features

```bash
python tools/rustgraph.py features reqwest json
```

Choosing the crate is half the answer. `cargo add reqwest`, call `.json()`, and
it does not compile — that lives behind a feature. `features` reports what is on
by default, what to opt into, and the exact Cargo.toml line.

**Implicit features matter as much as declared ones.** Cargo creates a feature
per optional dependency, so `chrono`'s `serde` feature appears in no features map
anywhere — it exists only because chrono optionally depends on serde, a crate
this list does not even carry. Recovering those needs every optional dependency,
not just the ones indexed here.

Covers **841 of 1,264** resolved crates (67%) — 831 with a declared features map,
the rest reachable only through implicit ones. Cargo internals (`__tls`, weak-dep
`foo?` syntax) are filtered out as noise.

```bash
python tools/rustgraph.py example reqwest
```

`example` prints the crate's own starting example, lifted verbatim from its
crates.io README, with its runtime constraint. **559 of 1,264 crates (44%)** have
one. An absent example means that README carried none, not that none exists —
`clap` is the notable gap, since its README defers to docs.rs.

## Dependency data

```bash
python tools/crates_deps.py <db-dump.tar.gz>   # then: rustgraph.py dag --write
```

`README.md` cannot supply crate identity, adoption, or dependencies. The
[crates.io database dump](https://static.crates.io/db-dump.tar.gz) can. It is
~1.8GB compressed and ~20GB unpacked, so it is streamed twice out of the tarball
and filtered to our nodes — nothing is ever unpacked to disk.

This raised crate identity from 536 to **1,264 of 1,702** projects and produced
**6,974** `depends_on` edges of all kinds — `dag` uses the 5,807 normal ones and
reaches 6,974 only with `--with-dev`. `index/crates-provenance.json` records which dump
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

## Tried and rejected

Two plausible improvements were built, measured, and removed. Both are recorded
here because both look obviously correct until you measure them.

**Ecosystem clustering.** The affinity graph does not cluster: one 134-node
component at lift >= 2, still one 35-node blob at lift >= 8. 135 crates with
partners is too small and too dense for community detection to mean anything.

**Mining crate READMEs.** The dump carries a crates.io description for every
resolved crate and README text for 93% of them — 1,218,775 words against the
17,555 in the curated one-liners, a 69x larger corpus. Extracting it changed
nothing. On a 16-query evaluation, no-extraction, description-only, and
description-plus-README-terms all scored **14/16**. README terms trade one answer
for another rather than adding any: they fix "persistence layer" and break
"REST API", which starts returning an Elasticsearch client whose README says REST
a great many times. Frequency alone surfaces "contributing" and "documentation";
IDF alone surfaces contributor names; whitelisting against curated vocabulary
fixes both and still does not help.

**Re-filing the signal-free categories.** The obvious next inference from the
above — `scraper` sits in `Web programming/Miscellaneous`, so fix the category —
does not survive measurement either. Only 73 projects (4.3%) sit in such
categories, and the diagnosis was wrong: `scraper` failed a query for web
scraping because the *broad parent* `Web programming` anchored it and popular
siblings inherited the score, not because its own leaf was named Miscellaneous.

What fixed it was keyword rarity, and no authoring at all. `scraping` is held by
one crate in this index; `web` is held by forty. Weighting a keyword match by how
many crates share it moved `scraper` from outside the top eight to fourth, and
made `ask "html scraping selectors"` return it first.

The generalisation across all three: reach for the derived signal before the
authored one, and measure the diagnosis before acting on it. Twice here the
obvious cause was not the real one.

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
