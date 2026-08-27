# repograph

Point it at a Rust checkout and ask it where things are.

```bash
python repograph/index.py /path/to/crate -o /tmp/idx
python repograph/repograph.py /tmp/idx ask "how is a png decoded"
```

Python 3, standard library only. Nothing is fetched, nothing is built: it reads
`.rs` files and writes JSON. Independent of the `awesome-rust` tooling in
`tools/` — it shares no code and can be split into its own repository whenever
that is worth doing.

## Commands

```
ask <question>     rank files and symbols, with file:line citations
sym <name>         everything known about one symbol
file <path>        what a file contains
callers <name>     which symbols reference it
coverage           documentation coverage, and whether to trust answers
eval <file>        measure retrieval against a question set
stats              index shape
selftest           check the query surface still behaves
```

## Two things to know before trusting it

**Read the top three, not the first.** Measured on 68 hand-written questions
across `syn`, `chrono` and `image`: the first answer is right **31%** of the
time, and the answer is in the top three **65%** of the time. That gap is the
single most useful fact about this tool, so `ask` prints three files and says so.

**Documentation is the ceiling, not the ranking.** Retrieval runs on prose, and
almost all the prose about a Rust symbol is its doc comment. Remove an item's
doc from the index and the chance of finding it from a description of it drops
to **1%** — a name, path and signature carry almost none of the words a person
types. `num_seconds_from_midnight` retains midnight/num/seconds and nothing
else. So `coverage` is a first-class command, and `ask` warns before answering
when a repository is thinly documented. That is not hedging; it is the finding.

## What was tried and does not work

Both were built, measured, and removed. They are recorded because both look
obviously correct until measured.

**Graph expansion.** Seed from the top lexical matches, then lift whatever the
reference graph says they are built alongside. Reachability is genuinely high —
15 of 18 held-out misses sit one hop from a seed. It ranks none of them. The
failure is fan-out, not edge quality: one hop lands on hundreds of candidates
and nothing separates them. This was tested twice on deliberately different
data, once with statistical co-occurrence edges (0.5% precise) and once with
parser edges (93% precise). Precision rose two hundredfold and the result did
not move.

**Centrality as a static prior.** Reference in-degree is the codebase analogue
of download counts, which are what make the sibling crate index work. On 30
hand-written questions it gained 6 points against a bar of 8 — unresolved, so
`evalgen.py` was built to resolve it. On 1,455 generated questions it is
monotonically harmful across all three generators: top-1 falls 40% → 39% → 34%
→ 25% → 13% as the weight rises. The 6 points were noise. `PRIOR_WEIGHT` stays
0 and the code stays, since flipping it is how the next person re-tests it.

## Measuring

`evalgen.py` generates questions with ground truth from the repository itself,
which is what makes a six-point difference resolvable at all:

```bash
python repograph/evalgen.py /tmp/idx -o /tmp/questions.json
python repograph/repograph.py /tmp/idx eval /tmp/questions.json
```

Three generators, labelled in the output because they carry different biases and
pooling them hides that: `module-doc` (a file's `//!` header), `doc-split` (an
item's first doc sentence) and `test-name` (words from a `#[test]` name).
`test-name` is the only one that works on undocumented code.

Every generated question holds out exactly the text it came from. This is not
optional and both failure modes were measured: leave the text in and a generated
question is an identity lookup scoring 66%; take it all out and the target
becomes unfindable at 1%. Neither measures retrieval. Holding out the first
sentence while keeping the rest is the usable middle.

**Generated sets are for tuning. Hand-written sets are for judging, and reading
one retires it** — once its failures have been studied, anything built afterwards
is shaped by them.

## Where the extractor is weak

Regex and brace counting, not a real parser. Measured before being relied on:
reference edges 93% precise on a 30-edge hand-check, symbol recall at or above a
manual count. It does not see items generated inside macros, which is why `syn`
reports 30% documentation against 83% for `chrono` — most of syn's AST types are
declared inside `ast_struct!`. A repository that generates much of its API this
way will under-index, and `coverage` is the place that shows it.

A Rust port is the obvious next step and deliberately not done yet; the Python
version needs no build, which is what makes it usable on sight.
