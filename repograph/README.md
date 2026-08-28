# repograph

Point it at a Rust checkout and ask it where things are.

```bash
python repograph/index.py /path/to/crate -o /tmp/idx
python repograph/repograph.py /tmp/idx ask "how is a png decoded"
```

Python 3, standard library only. Nothing is fetched, nothing is built: it reads
`.rs` files and writes JSON.

`AGENT-PROMPT.md` says all of this in one self-contained page, for pasting to an
agent that does not have the repo yet. `PROTOCOL.md` is the sequence to follow
once it does. Independent of the `awesome-rust` tooling in
`tools/` — it shares no code and can be split into its own repository whenever
that is worth doing.

## Commands

```
ask <question>     rank files and symbols, with file:line citations
context <question> the same candidates with their docs attached, in one call
                    [--files N] [--source] [--budget CHARS]
sym <name>         everything known about one symbol
file <path>        what a file contains
callers <name>     which symbols reference it
coverage           documentation coverage, and whether to trust answers
eval <file>        measure retrieval against a question set
stats              index shape
selftest           check the query surface still behaves
```

## Using it as an agent

`PROTOCOL.md` is the sequence: check `coverage`, ask with `context`, read all
three candidates, recover when none fit, confirm against `--source`, cite
`file:line`. Every step in it exists because of a measurement, and the
measurement is quoted, so a reader can tell when a step stops applying.

## `ask` ranks; `context` hands over the material

`ask` prints a ranking. `context` prints the candidates with their
documentation, signatures and locations attached, so a reader gets everything
needed to answer without opening files:

```bash
python repograph/repograph.py /tmp/idx context "how is a png decoded" --source
```

That split exists because of one number. On questions written blind, the first
answer is right 28% of the time and the right answer is in the top three 54% of
the time. A person reading
a ranked list experiences that 25-point gap as a wrong answer. A reader handed
all three candidates with their docs attached just picks the right one, which
turns the top-three figure into the accuracy that actually applies. `--source`
adds the code itself, `--budget` caps the output.

## Two things to know before trusting it

**Read the top three, not the first.** Measured on 100 questions written blind
by agents that had never seen this tool, across `toml`, `url`, `serde_json`,
`tracing-subscriber` and `flate2`: the first answer is right **28%** of the
time, and the answer is in the top three **54%** of the time. That gap is the
single most useful fact about this tool, so `ask` prints three files and says so.

**Coverage counts library code, and only what is really public.** Three things
had to be fixed before the number meant anything, and the first was a hypothesis
that turned out to be wrong -- syn's low score is *not* items hidden inside
`ast_struct!` macros, which extract fine. It was that `benches/`, `examples/`
and `tests/` were indexed as if they were the library (20% of syn's symbols),
that a `pub fn` inside a private `mod` counted as API, and that `pub(crate)`
counted as `pub`. Correcting those moved chrono from 83% to 93%, image to 96%
and regex to 99% -- which is what those crates actually look like. syn stays at
34%, and that is now honest: 564 of its undocumented public functions are the
generated visitors in `src/gen/`.

**Documentation is the ceiling, not the ranking.** Retrieval runs on prose, and
almost all the prose about a Rust symbol is its doc comment. Remove an item's
doc from the index and the chance of finding it from a description of it drops
to **1%** — a name, path and signature carry almost none of the words a person
types. `num_seconds_from_midnight` retains midnight/num/seconds and nothing
else. So `coverage` is a first-class command, and `ask` warns before answering
when a repository is thinly documented. That is not hedging; it is the finding.

## Verified examples: the one thing here that is not probabilistic

```bash
python repograph/repograph.py /tmp/idx example from_str
```

Everything else in this tool is a guess with a hit rate. A doc example is not.
`cargo test --doc` compiles and runs the examples in a crate's documentation,
so the crate's own CI has already proved them. The failure mode becomes
**absent** rather than **wrong**, which no amount of ranking work achieves --
`chrono` carries 257 examples, 256 of them compiled and run.

Each is labelled by what it actually demonstrates, which is not the same as
whether cargo touched it:

| bucket | fences | meaning |
|---|---|---|
| VERIFIED | bare, `rust` | compiled and run; copyable |
| COMPILES ONLY | `no_run` | builds, never executed |
| NOT VERIFIED | `ignore`, `text`, other languages | nobody checked it |
| DEMONSTRATES FAILURE | `compile_fail`, `should_panic` | **meant to break** |

`should_panic` is the trap: cargo runs it and it passes, while the code shows a
crash. A flag meaning "cargo verified this" would hand that over as working
code, so the buckets classify by intent and only VERIFIED is presented as
something to copy.

**Two caveats, both found by compiling the extracted output rather than
assuming.** Ten extracted examples were built against the real crate: eight
compiled immediately, and the two failures needed a feature flag and a
dev-dependency -- not extraction bugs. Doc tests run inside the crate with
every feature and dev-dependency present, and a reader copying the code has
neither. So `example` now prints the required feature where it can detect one
(`#[cfg(feature = "raw_value")] mod raw;` gates 94 serde_json symbols) and says
plainly that the crate's test setup is more generous than yours.

That validation also caught the worst bug of the feature. Hidden doc-test lines
begin `# `, and an early version stripped every line starting with `#` -- which
silently deleted `#[derive(Deserialize)]` from ten examples, producing code
that could never compile while claiming to be verified. Hidden lines are now
unhidden and attributes kept.

### `start` -- how do I use this crate

```bash
python repograph/repograph.py /tmp/idx start
```

A crate's `lib.rs` header is its author's own getting-started guide, and the
fenced blocks in it are the code they chose to lead with -- compiled by the
same doc tests as everything else. Selecting it needs no ranking, which is the
point: **the author already chose.** Runnable examples are shown first, because
headers often open with a JSON blob or a shell snippet to set the scene and
leading with an unverified fragment wastes the only advantage the command has.

Three of eight crates tested (`regex`, `url`, `clap_builder`) have no module
header at all, so `start` falls back to the examples on the most widely
referenced public items -- a fixed property of the crate, not an answer to a
query.

## Does an agent with this tool beat an agent with grep? Measured: no

Nine subagents, three arms on three crates spanning 4x in size, 10 blind
questions each, matched 30-call budgets, mechanically scored.

| crate | A protocol + tool | B tool only | C grep only |
|---|---|---|---|
| flate2 7.3k | 10/10, 13 calls | 10/10, 12 calls | 10/10, 10 calls |
| serde_json 23k | 10/10, 7 calls | 10/10, 15 calls | 10/10, 8 calls |
| tracing-subscriber 29k | 7/10, 13 calls | 8/10, 13 calls | 9/10, 15 calls |
| **pooled** | **27/30** | **28/30** | **29/30** |

**The three arms disagree on 3 of 30 questions.** Every paired comparison is
noise: protocol worth z = 0.58, tool worth z = 1.41. The honest reading is not
"grep wins" but "at these sizes, on these questions, a capable agent arrives at
the same answers with the tool, with the tool plus a protocol, or with neither".

That is worth stating plainly because it deflates the obvious claim. **This tool
has no measured advantage over ripgrep for an agent that can read code.** Two
crates saturated at 100% for every arm, so only `tracing-subscriber`
discriminated at all, and there by three questions.

The prediction fixed before running was that grep would win on the small crate
and lose on the large one. It won at every size and by the widest margin on the
largest, which is the opposite of the reasoning behind the prediction.

**One confound, and it is serious.** The blind questions were written by agents
who found their own ground truth by grepping and reading source. So the
questions are answerable-by-grep *by construction*, which structurally
advantages arm C. A fair test needs questions whose answers were established
some other way -- from a changelog, an issue tracker, or a human who already
knew the codebase.

What the agents said, consistently, is more useful than the scores:

- The **structural** commands did the work. "The genuinely productive commands
    were the structural ones"; `file src --all` plus `sym ignore_value` resolved
    questions outright. `PROTOCOL.md` treats those as the *recovery* path, which
    looks like the wrong order.
- `context` is too heavy. Four questions batched produced 47.6KB and had to be
    spilled to a file. `ask` was "far more economical and sufficient for
    shortlisting".
- Ranking put the right file at rank 2 repeatedly, and "read all three" is what
    saved it -- the one protocol instruction that demonstrably earned its place.
- Crate-level `coverage` hides what matters. `tracing-subscriber` reports 94%
    documented while its filter internals carry almost no docs, and that is
    exactly where retrieval failed. Coverage should be reported per directory.

## The confound, settled: it depends on whether you can name the thing

The null result above is bounded by how its questions were made -- agents wrote
them by grepping and reading, so the answers were grep-findable by
construction. Git breaks that. A commit subject is the author's account of
intent, written before anyone went looking; the files it touched are recorded
by version control. Neither half owes anything to lexical overlap.

210 commits from `chronotope/chrono`, subject used verbatim as the question,
changed files as the answer:

| | questions | top-1 | top-3 |
|---|---|---|---|
| subject names a symbol in the crate | 182 | 43% | 63% |
| subject is plain prose | 28 | **18%** | **21%** |
| all | 210 | 40% | 58% |

The headline 40/58 is higher than the blind sets' 28/54, and the split says why:
**it is entirely identifier-driven.** When a question names something the crate
calls by that name, the tool finds it. When a question only *describes*
behaviour, it lands at 21% in the top three -- against roughly 6% for guessing
among fifty files, so better than chance and not much use.

That is the honest shape of the thing, and it is worth more than either average.
Description-based search was the whole premise; description is exactly where it
is weakest. It also explains the null result against grep rather than
contradicting it, since grep likewise needs you to know the word.

(28 prose-only questions is a small sample and the interval around 18/21 is
wide. The direction is clear; the precise number is not.)

The questions and the script that builds them are in `eval/git/`, so this can be
rerun against any repository with history.

## Four fixes the experiment produced

The scores said nothing; the agents' reports said plenty, and all four of these
come from what three independent agents complained about.

**`coverage` now breaks down by directory, counting every symbol.** The
headline counts public API and averages across the crate, which hid the thing
that mattered: `tracing-subscriber` documents 94% of its public API and 18% of
what is in `src/filter`, and `src/filter` is where the test questions failed.
`src/filter/directive.rs` documents 3 of its 42 symbols while exporting one
documented public item -- a public-API measure calls that perfect and a
searcher finds nothing. Directories under 25% are flagged as places to navigate
rather than interrogate.

**`context` is a third of its former size.** It showed six symbols per
candidate with long doc extracts; four batched questions produced 47KB that an
agent had to spill to a file and read back. Now three symbols and a short doc
per candidate: the two queries measured went from 4,121 and 9,022 characters to
2,694 and 3,553, with all three candidates still intact.

**The confidence percentage is replaced by the words that missed.** It used to
print `CONFIDENCE high`, which was measurably inverted -- on 100 blind
questions `high` was right 14% of the time and `LOW` 29%, because the way to
match many words at once is to hit boilerplate repeated across files. Renaming
it to `QUERY MATCH` was honest but still useless. It now prints **NOT IN THIS
INDEX** followed by the words that appear nowhere in the repository, which is
the actionable half. The agent who asked why colours vanish when piping to a
file is told immediately that `tty` and `terminal` are absent -- because
`tracing-subscriber` has no terminal detection at all, so the premise was
wrong.

**`PROTOCOL.md` is reordered.** It used to say: ask, then navigate structurally
if that fails. Every agent that succeeded did the opposite -- `file src --all`
first for the shape, `sym` when they had a name, ranked search afterwards. The
protocol now says that, and opens by admitting the tool has no measured
advantage over grep so a reader can choose on cost rather than obligation.

## What was tried and does not work## What was tried and does not work

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

**Tuning the ranker.** Every knob -- stemming, per-field weighting, IDF
exponent, term frequency saturation, file aggregation, stop words -- was swept
against 1,455 generated questions and then judged on five crates the tuning
never saw. **All of them were already at their optimum.** Stemming is flat at
best, module-header weight above 0.5 costs 11 points, averaging ten symbols per
file costs 7, unsaturated term frequency costs 10.

Field weighting is worth recording carefully, because the first measurement of
it was invalid. A failed edit left the weighted bag builder wired only into the
eval holdout path, so the weights changed nothing except `doc-split` questions,
and that artefact was initially written up as a finding about identity lookups.
With the bug fixed, weighting an item's name 12x and its path 4x is a real
effect on the tuning crates: +2 points, net +23 questions, McNemar z = 2.66.

It still does not ship, because on the five held-out crates the same setting
scores **-0.4 points, net -3, z = 0.36**. The gain was overfitting to three
repositories, which is the one thing a held-out set exists to catch.

So the ranker has no tuning headroom, which is what `coverage` has been saying
all along: the limit is how much prose a repository contains, not how it is
weighed. Five further crates -- `clap_builder`, `rand`, `rayon`, `regex`,
`indexmap`, 1,536 questions -- were indexed as a held-out set and remain unspent,
because nothing was found that needed judging.

## Getting a judgment set you can trust

The hard part of measuring retrieval is not running the numbers, it is having
questions nobody tuned against. Hand-writing them does not scale -- thirty
questions cannot resolve a two-point difference -- and writing them yourself
after watching the tool fail is not measurement at all.

What works: **give a subagent one crate and forbid it from seeing this tool.**
It explores the source, writes questions in its own words, and verifies each
answer by opening the file. It cannot bias toward what the tool can find,
because it has never seen the tool. `repograph/eval/blind/` holds five such
sets, 100 questions, produced that way.

The instructions that mattered:

- Read only the crate. Do not read `repograph`, or any eval file.
- Phrase questions the way a developer talks, **not** the way the crate's own
    documentation is worded -- otherwise the question is answerable by text
    matching and measures nothing.
- Verify every answer by opening the file. Discard anything unverifiable.
- Spread across difficulty and across modules.

The agents took that further than asked. One reworded a question because it had
quoted a string appearing verbatim in a source comment; another avoided whole
topics in `lib.rs` because the answers lived in prose and would be "answerable
by text matching alone". The resulting questions are markedly harder than the
ones written by hand:

> *if the authority contains more than one @ sign, which one splits the
> credentials from the host*
>
> *my colored log output goes away when I pipe it into a file on CI - what is
> deciding to drop the color codes?*

Per-crate results vary far more than any average suggests, which is why the
harness reports them separately:

| crate | top-1 | top-3 |
|---|---|---|
| url | 50% | 70% |
| flate2 | 35% | 75% |
| tracing-subscriber | 30% | 50% |
| serde_json | 15% | 50% |
| toml | 10% | 25% |
| **pooled** | **28%** | **54%** |

`toml` scores worst for a reason the tool cannot fix: it delegates string
escaping, datetimes and output to *other crates*, so several natural questions
have no answer inside the repository being indexed.

These five sets are now **spent as judgment** -- their failures have been read,
so anything tuned afterwards is shaped by them. They stay in the repository as
a fixed regression benchmark. A new judgment needs new sets, and now that is an
afternoon of subagent time rather than an afternoon of yours.

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
