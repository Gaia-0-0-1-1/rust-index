# Rust crate index — how to use it

Paste this whole file to an agent that needs to pick Rust crates.

---

This repo is the `awesome-rust` curated list (~1,700 Rust projects) turned into a
queryable index. Instead of grepping a 2,500-line README, ask it what to use and
get back crates with real adoption numbers and exact source citations.

## Get it

```bash
git clone https://github.com/Gaia-0-0-1-1/rust-index.git
cd rust-index
python tools/rustgraph.py ask "async http client"
```

Private repo — you need access to `Gaia-0-0-1-1/rust-index`. It clones straight onto
the `agent-index` branch, so there is no checkout step.

Python 3, stdlib only. No install, no network, no build: the index is committed,
so a fresh clone answers immediately. `cargo` is needed only to regenerate it.
If `python` is not on your PATH, try `py` or `python3` — on Windows it varies.

Already have a checkout? The commands below run from its root; ignore the clone.

Read `AGENTS.md` in the repo root for the full reference.

## Commands

```
ask <query>          rank projects for a need, with alternatives + install line
cat "<path>"         list a category (its members are mutual alternatives)
node <name|crate>    everything known about one project (JSON)
recipe [name]        archetype stacks (see list below)
stack <crate>        what a crate is really built alongside (from dep graph)
dag                  dependency strata and most load-bearing crates
stats                index shape
selftest             verify the index still answers correctly
```

Recipes: `cli`, `web-service`, `tui`, `parser`, `data`, `http-client`, `wasm`.

## What it knows

1,702 projects, 355 categories, 1,264 resolved to crates.io with download counts,
and 6,974 dependency edges from the crates.io database dump.

## Rules

- **Check `CONFIDENCE` before trusting an order.** Every `ask` prints
    `CONFIDENCE high` or `CONFIDENCE LOW` with the share of your query the index
    matched. LOW means the results are candidates to verify, not a ranking.
    `NO MATCH` (exit 1) means nothing scored at all.
- **Cite, don't assert.** Every result carries `README.md:<line>`. Quote it.
- **Never hand-edit anything in `index/`.** It is generated. Regenerate it with
    `cargo run --bin index`.
- **Never run a `cargo add` marked `UNVERIFIED` or `?` without checking
    crates.io first.** 146 crate names are inferred from a repo name, and they are
    wrong whenever the repo and crate names differ — `curl-rust` ships `curl`.
- **Query capabilities, not brands**: "http client", "argument parsing",
    "terminal ui". On `NO CATEGORY MATCHED THIS WORDING` the results are unranked
    guesses — browse with `cat` and retry in the list's own words.

## Known limits

- 438 projects have no crate identity: applications, resources, and anything
    never published to crates.io. They still rank and cite correctly.
- Multi-concept queries return the dominant concept only. "async http client with
    rustls" gives HTTP clients; query `rustls` separately.
- Download counts are a snapshot. See `index/crates-provenance.json` for which
    crates.io dump they came from.
- Some ecosystem staples are missing from the underlying list entirely, including
    `serde`, `anyhow` and `thiserror`. `recipe` flags these as NOT IN THIS LIST
    rather than pretending they are covered.

## If you change README.md

Run `cargo run --bin index` and commit the `index/` diff in the same commit, or
CI fails. Three gates guard this branch: bullet accounting, index freshness, and
`selftest`.
