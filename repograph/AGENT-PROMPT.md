# repograph — how to use it

Paste this whole file to an agent that needs to work in an unfamiliar Rust
codebase.

---

`repograph` indexes any Rust checkout and answers questions about it. Python 3,
standard library only. No network, no build, no install — it reads `.rs` files
and writes JSON, in a second or two.

```bash
git clone https://github.com/Gaia-0-0-1-1/rust-index.git
cd rust-index
python repograph/index.py /path/to/some/crate -o /tmp/idx
python repograph/repograph.py /tmp/idx start
```

Private repo — you need access to `Gaia-0-0-1-1/rust-index`. It clones onto the
`agent-index` branch, so there is no checkout step. If `python` is not on your
PATH try `py` or `python3`.

## Read this before you rely on it

**Searching it is no better than ripgrep.** Nine agents, three arms, three
crates, thirty questions: with this tool and its protocol 27/30, with the tool
alone 28/30, with nothing but grep 29/30. The differences are noise.

**And it only works if you already know the vocabulary.** Measured against 210
git commits, where the question is the author's own commit subject and the
answer is the files that commit touched:

| the question… | top-1 | top-3 |
|---|---|---|
| names a symbol the crate has | 43% | 63% |
| only describes the behaviour | **18%** | **21%** |

Describing behaviour is what a search tool is supposed to be *for*, and that is
where this one is weakest. Use it where it is cheaper than grep and use grep
where grep is cheaper — which is often.

## What it gives you that grep does not

**Verified code.** `cargo test --doc` compiles and runs the examples in a
crate's documentation, so the crate's own CI has already proved them. This is
the one part of the tool that is not a guess: the failure mode is that no
example exists, never that the example is wrong.

```bash
python repograph/repograph.py /tmp/idx start            # how to use this crate
python repograph/repograph.py /tmp/idx example from_str # working code for one item
```

Every example is labelled by what it demonstrates. Only **VERIFIED** is
copyable. **DEMONSTRATES FAILURE** means `compile_fail` or `should_panic` —
code written to break, which passes its own test while doing so. Examples also
report the `#[cfg(feature)]` they need, because doc tests run with every
feature and dev-dependency present and you will not have them.

**An honest map of where searching works.** `coverage` breaks down per
directory over every symbol, not just the public API. `tracing-subscriber`
documents 94% of its public API and 18% of what is in `src/filter`, and
`src/filter` is exactly where questions fail. Anything under 25% is a place to
navigate, not interrogate.

## Commands

```
start              how to use this crate, from its author's own header
example <name>     working code from a doc comment, labelled by verification
coverage           where searching will and will not work
file <path>        a directory's contents, or a file's symbols   [--all]
sym <name>         everything known about one symbol
callers <name>     which symbols reference it
ask <question>     ranked files and symbols with file:line
context <question> the same candidates with docs attached   [--source]
eval <file>        measure retrieval against a question set
stats · selftest   index shape · check the tool still behaves
```

## Rules

- **Read `PROTOCOL.md`.** It is ordered by what worked when agents were watched
    using this, which is not the order you would guess: structure first
    (`file`, `sym`), ranked search after.
- **Read all three candidates, never just the first.** First place is right 28%
    of the time and the answer is in the top three 54% of the time, on blind
    questions. That gap is the most useful fact about the tool.
- **`NOT IN THIS INDEX` is the most valuable thing it prints.** Those words
    appear nowhere in the repository. Usually your phrasing is wrong; sometimes
    your premise is. An agent asked why colours vanish when piping to a file and
    was told `tty` is absent — because `tracing-subscriber` has no terminal
    detection at all.
- **Cite `file:line`.** Every candidate carries one. This tool is wrong often
    enough that an unchecked claim is a real risk.
- **Confirm against the source, however is cheapest.** A targeted grep is often
    cheaper than `context --source`, and several agents preferred it. The point
    is that you read the code.
- **It indexes one repository.** Dependencies are invisible. `toml` delegates
    string escaping, datetimes and output to three other crates, so questions
    about those have no answer inside it however well you search.
- **Tests, benchmarks and examples are excluded from ranking**, though `file`
    and `sym` still reach them.
- **There is no fuzzy matching.** A misspelling returns nothing, not a near
    miss.

## If you want to measure it yourself

Three instruments, and they are not interchangeable:

- `evalgen.py` generates hundreds of ground-truthed questions from a repository
    itself. For **tuning** at scale.
- `eval/blind/` holds 100 questions written by agents who had never seen the
    tool. For **judgment** — and spent, since their failures have been read.
- `eval/git/` builds questions from commit history, where neither the question
    nor the answer was chosen by searching. The only instrument whose ground
    truth no search produced.

Reading a judgment set's failures retires it. `README.md` records what was
measured and, at greater length, what was measured and thrown away.
