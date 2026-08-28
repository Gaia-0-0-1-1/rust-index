# How to use repograph to answer a question about a codebase

Written for an agent. Every step exists because of a measurement, and the
measurement is given, so you can tell when a step stops applying.

## Read this first

**This tool has no measured advantage over ripgrep for an agent that can read
code.** Nine agents, three arms, three crates, thirty blind questions: with the
tool and this protocol 27/30, with the tool alone 28/30, with nothing but grep
29/30. The differences are noise.

So do not reach for it out of obligation. Use it where it is genuinely cheaper
than grep -- orienting in a repository you have never seen, and turning a name
you already have into a location -- and use grep where grep is cheaper, which
is often. The steps below are ordered by what worked when agents were watched
doing this, which is not the order the tool's author expected.

## 0. Index the repository, once

```bash
python repograph/index.py /path/to/crate -o /tmp/idx
```

Reads `.rs` files and writes JSON. No network, no build, a second or two.

## 1. Find out where searching will and will not work

```bash
python repograph/repograph.py /tmp/idx coverage
```

Retrieval runs on prose, and nearly all the prose about a Rust symbol is its
doc comment. **Strip an item's doc and the chance of finding it from a
description falls to 1%** -- a name and signature carry almost none of the
words a person types.

Read the **per-directory** breakdown, not the headline. Averages lie here:
`tracing-subscriber` documents 94% of its public API and 18% of what is in
`src/filter`, and `src/filter` is exactly where a test agent's questions
failed. Anything under 25% is a directory to navigate structurally rather than
ask questions about.

## 2. Get the shape of the crate

```bash
python repograph/repograph.py /tmp/idx file src --all
```

Every file with its symbol count and module header. This was the single most
productive command in testing -- agents who ran it resolved questions outright
that ranked search did not answer. Do it before asking anything.

If you already have a name, go straight to it. This is where the index most
clearly beats grep, because it knows a definition from a mention:

```bash
python repograph/repograph.py /tmp/idx sym PngDecoder
python repograph/repograph.py /tmp/idx callers decode_frame
```

## 3. Ask, once you know roughly where to look

```bash
python repograph/repograph.py /tmp/idx context "how is a png decoded"
```

`context` gives three candidate files with their module docs, signatures and
`file:line`. `ask` is the leaner version when a shortlist is all you need.

If the output says **NOT IN THIS INDEX**, those words appear nowhere in the
repository. That is the most useful thing the tool says: usually your phrasing
is wrong, sometimes your premise is. An agent asked why colours vanish when
piping to a file, and `tty` and `terminal` were absent because
`tracing-subscriber` has no terminal detection at all.

## 4. Read all three candidates. Do not take the first

**The first candidate is right 28% of the time. The right answer is in the top
three 53% of the time.** Measured on 100 questions written blind across five
crates.

This is the one instruction here with demonstrated value: agents reported the
correct file landing at rank 2 repeatedly, and reading all three is what saved
them. Taking the first throws away half the tool's accuracy.

Per-crate accuracy ranged from 10% to 50% top-1, so treat these as a shape, not
a promise about the repository in front of you.

## 5. When none of them fit

Roughly half the time. Normal, not an error.

- **Rephrase in the repository's own words.** The candidates show you how this
    codebase talks. Your words may simply not be its words.
- **Go back to step 2.** Structural navigation beats a second guess at phrasing.
- **Consider that the answer is elsewhere.** Crates delegate: `toml` hands
    string escaping, datetimes and output to three other crates, so questions
    about those have no answer inside it however well you search. Say so rather
    than returning the closest thing this repository happens to hold.

## 6. Confirm against the source, however is cheapest

The index tells you where something probably is; only the source tells you what
it does. `context --source` will show it, but **a targeted grep is often
cheaper** and several agents preferred it. Use whichever costs less. The point
is that you read the code, not which tool put it in front of you.

## 7. Cite `file:line`

Every candidate carries one. Quote it. A claim about a codebase without a
citation cannot be checked, and this tool is wrong often enough that unchecked
claims are a real risk.

## What the tool will not do

- **Tests, benchmarks and examples are excluded from ranking.** Someone asking
    where dates are added means the implementation, not `test_date_add_assign`
    beside it. `file` and `sym` still reach them.
- **It does not read code for you.** It finds candidates. Steps 4 and 6 are
    yours.
- **It has no fuzzy matching.** A misspelling returns nothing rather than a
    near miss.
- **It indexes one repository.** Dependencies are invisible.
