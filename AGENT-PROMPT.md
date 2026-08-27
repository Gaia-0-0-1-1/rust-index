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
features <crate> [x] which Cargo feature to turn on, and the Cargo.toml line
example <crate>      the crate's own starting example (44% of crates have one)
dag                  dependency strata and most load-bearing crates
stats                index shape
selftest             verify the index still answers correctly
eval                 measure retrieval on held-out queries (never fails)
```

Recipes: `cli`, `web-service`, `tui`, `parser`, `data`, `http-client`, `wasm`.

## What it knows

1,702 projects, 355 categories, 1,264 resolved to crates.io with download counts,
and 5,807 runtime dependency edges from the crates.io database dump (6,974
including dev-dependencies, which `dag` excludes by default).

## Rules

- **Check `CONFIDENCE` before trusting an order.** Every `ask` prints
    `CONFIDENCE high` or `CONFIDENCE LOW` with the share of your query the index
    matched. LOW means the results are candidates to verify, not a ranking.
    `NO MATCH` (exit 1) means nothing scored at all.
- **Cite, don't assert.** `ask`'s best matches and `recipe` steps carry
    `README.md:<line>` — quote it. Other blocks (`stack`, `cat`, `features`,
    `example`, and ask's own alternatives list) do not cite; do not invent one.
- **Never hand-edit anything in `index/`.** It is generated. Regenerate it with
    `cargo run --bin index`.
- **Never run a `cargo add` marked `UNVERIFIED` or `?` without checking
    crates.io first.** 515 names are repo-derived rather than read from
    crates.io — 146 resolved to a crate by download count, plus 369 with no
    resolved identity at all. They are wrong whenever repo and crate names
    differ: `curl-rust` ships `curl`.
- **Query capabilities, not brands**: "http client", "argument parsing",
    "terminal ui". Naming a crate outright also works, but only when the name is
    the whole query — `wgpu` finds wgpu, while `websocket server` is read as a
    capability, which is what you want.
- **`ask` returns alternatives, not a set.** The results share a category and
    compete with each other. Install one, not all of them.
- **Heed `DORMANT`.** A result marked so has not published in three years or
    more; downloads are cumulative, so a dead crate keeps its popularity. Check
    it is still maintained before depending on it.
- **Read the top three, not just the first.** Measured on 50 held-out queries:
    the first result is right **59%** of the time, and the right answer is in the
    top three **80%** of the time and returned at all **86%** of the time. The
    results are mutual alternatives from one category, so scanning three costs
    nothing. Run `python tools/rustgraph.py eval` to reproduce this.
- **`CONFIDENCE high` is weakly calibrated.** Roughly a quarter of `high`
    verdicts were wrong and about half of `LOW` ones were right. Read `high` as
    "matched most of your query", not "this is correct". On off-distribution
    wording almost everything comes back `LOW`, which is the tool telling you
    the truth rather than failing.

## Known limits

- 438 projects have no crate identity: applications, resources, and anything
    never published to crates.io. They still rank and cite correctly.
- Multi-concept queries return the dominant concept only. "async http client with
    rustls" gives HTTP clients; query `rustls` separately.
- Download counts are a snapshot. See `index/crates-provenance.json` for which
    crates.io dump they came from.
- **The list, not the ranking, is the binding limit.** Of 20 blindly-written
    queries, 6 named something `awesome-rust` does not carry at all -- password
    hashing, `uuid`, `base64`, spreadsheets, big integers, state machines. Expect
    roughly **30% of ordinary questions to hit a crate that is simply absent**.
    When results look unrelated, that is the likely cause; check crates.io
    directly rather than accepting the closest thing the list happens to hold.
- Some ecosystem staples are missing from the underlying list entirely, including
    `serde`, `anyhow`, `thiserror`, `uuid` and `embedded-hal`. `recipe` flags the
    ones it names as NOT IN THIS LIST; for the rest, `node <crate>` returning
    `"matches": 0` means the list does not carry it, not that it does not exist.
- Plurals and British spelling are folded (`arguments`/`argument`,
    `serialisation`/`serialization`), but there is **no fuzzy matching**. A
    misspelling is a dead end: `webscoket servr` returns NO MATCH.

## If you change README.md

Run `cargo run --bin index` and commit the `index/` diff in the same commit, or
CI fails. Three gates guard this branch: bullet accounting, index freshness, and
`selftest`.
