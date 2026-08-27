# How to use repograph to answer a question about a codebase

Written for an agent. Every step exists because of a measurement, and the
measurement is given, so you can tell when a step stops applying.

## 0. Index the repository, once

```bash
python repograph/index.py /path/to/crate -o /tmp/idx
```

Reads `.rs` files and writes JSON. No network, no build, a second or two.

## 1. Ask whether this repository is searchable at all

```bash
python repograph/repograph.py /tmp/idx coverage
```

Retrieval here runs on prose, and nearly all the prose about a Rust symbol is
its doc comment. **Strip an item's doc and the chance of finding it from a
description of it falls to 1%** — a name and signature carry almost none of the
words a person types.

So the number `coverage` prints is not decoration, it is the ceiling:

- **70%+** — description-style questions should work. Proceed.
- **50–70%** — expect gaps in the undocumented corners.
- **under 50%** — the tool prints `THIN`. Retrieval will be weak and no amount
  of rephrasing fixes it, because the text is not there. Skip to step 4 and
  navigate structurally instead of searching.

## 2. Ask the question

```bash
python repograph/repograph.py /tmp/idx context "how is a png decoded"
```

`context` is the entry point, not `ask`. `ask` ranks; `context` hands over the
candidates with their module docs, signatures, documentation and `file:line`
already attached, which is everything needed to choose without opening files.

## 3. Read all three candidates. Do not take the first

**The first candidate is right 28% of the time. The right answer is in the top
three 54% of the time.** Measured on 100 questions written blind across five
crates. That 26-point gap is the single most useful fact about this tool, and
the whole reason `context` returns three.

Taking the first candidate throws away half the tool's accuracy. Read all
three, then decide.

Per-crate accuracy ranged from 10% to 50% top-1 in that measurement, so treat
these figures as a shape, not a guarantee for the repository in front of you.

## 4. When none of the candidates fit

This happens roughly half the time. It is the normal case, not an error, and it
is where an agent beats one-shot search. In rough order of cost:

**Reformulate in the repository's own vocabulary.** The candidate output shows
you how this codebase words things. A question phrased in those words will
match; a question phrased in yours may not.

**Ignore `QUERY MATCH` when deciding whether an answer is right.** It reports
how many of your words appear in the top result, and that is all it reports. On
100 blind questions a *high* match was right 14% of the time and a low match
29% — the signal is inverted, because the way to match many words at once is to
hit repeated boilerplate. Three of flate2's decoders share a stock sentence
about trailing data, so every question about trailing data matches all three
strongly and is answered by none of them. A low match is still a reasonable
prompt to rephrase, since it means the index did not understand your wording.
A high match means nothing about correctness.

**Navigate structurally.** `file` on a directory lists everything in it with
symbol counts, so it is the cheapest way to see the shape of a crate.

```bash
python repograph/repograph.py /tmp/idx file src/codecs      # what is in here
python repograph/repograph.py /tmp/idx sym PngDecoder       # one symbol
python repograph/repograph.py /tmp/idx callers decode_frame # who reaches it
```

`callers` is the strongest of these when you already have one true symbol:
walking the reference graph from something you know beats guessing new words.

**Consider that the answer is not in this repository.** Crates delegate. `toml`
hands string escaping, datetimes and output to three other crates, so questions
about those have no answer inside it however well you search. If `coverage`
looked healthy and several rephrasings all miss, the likeliest explanation is
that the code lives in a dependency. Say so rather than returning the closest
thing this repository happens to hold.

## 5. Confirm before answering

```bash
python repograph/repograph.py /tmp/idx context "..." --source
```

`--source` includes the code itself. Read it. The index tells you where
something probably is; only the source tells you what it does. This step is
what separates an answer from a guess, and it is cheap.

## 6. Cite `file:line`

Every candidate carries one. Quote it. A claim about a codebase without a
citation cannot be checked, and this tool is wrong often enough that unchecked
claims are a real risk.

## What the tool will not do

- **Tests, benchmarks and examples are excluded from ranking.** Someone asking
  where dates are added means the implementation, not `test_date_add_assign`
  beside it. `file` and `sym` still reach them.
- **It does not read code for you.** It finds candidates. Steps 3 and 5 are
  yours.
- **It has no fuzzy matching.** A misspelling returns nothing rather than a near
  miss.
- **It indexes one repository.** Dependencies are invisible.
