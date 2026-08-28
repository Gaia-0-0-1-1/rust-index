"""Ask a codebase where things are.

    python repograph/index.py /path/to/repo -o IDX [--lang go]
    python repograph/repograph.py IDX ask "how is a png decoded"

Rust and Go. Nothing here knows which: the language plugin supplies the words
it uses, what its examples are verified by, and what gates a symbol.

Commands
    ask <question>     rank files and symbols for a question, with citations
    context <question> the same candidates with their documentation attached,
                        in one call, for an agent to read and decide
                        [--files N] [--source] [--budget CHARS]
    sym <name>         everything known about one symbol
    start              how to use this crate or package, from its own docs
    example <name>     working code from its documentation, labelled by what
                        actually verifies it in this language
    file <path>        what a file contains, or what a directory holds [--all]
    callers <name>     which symbols reference it
    coverage           documentation coverage, and whether to trust answers
    eval <file>        measure retrieval against a question set
    stats              index shape
    selftest           check the query surface still behaves

Read the top three, not the first. Measured on 100 questions written blind by
agents that had never seen this tool, across five crates: the first answer is
right 28% of the time and the answer is in the top three 54% of the time. That
gap is the most useful fact about this tool.
"""
import io
import json
import math
import os
import re
import sys

SPLIT = re.compile(r'[^A-Za-z0-9]+')
CAMEL = re.compile(r'(?<=[a-z0-9])(?=[A-Z])')

# Reference in-degree as a static ranking prior -- the codebase analogue of the
# download counts that carry the crate index. Implemented and OFF: judged once
# on 30 blind questions it gained 6 points of top-1 against a pre-registered bar
# of 8, which on 30 questions is two answers and inside the noise. Unresolved
# rather than refuted, and `evalgen.py` exists to resolve it. Flip via --prior.
PRIOR_WEIGHT = 0.0

# How words are matched. Swept against 1,455 generated questions over three
# crates, then judged on five crates the tuning never saw. Everything below is
# at its measured optimum; the values are what the tool shipped with and
# nothing beat them on held-out data.
#
# Swept and rejected: stemming (flat at best), module-header weight above 0.5
# (-11 points at 6.0), averaging more than one symbol per file (-7 at top-10),
# raw term frequency instead of saturated (-10), rustdoc boilerplate as stop
# words (no effect).
#
# Field weighting deserves its own note, because the first attempt at measuring
# it was wrong. A failed string replacement left `build_bag` wired only into the
# eval holdout path, so W_NAME and friends affected nothing but `doc-split`
# questions -- and the resulting "gain appears only on doc-split" was read as
# evidence about identity lookups when it was evidence about a bug. With the
# weights actually applied, W_NAME=12 with W_PATH=4 gains +2 points on the
# tuning crates, net +23 questions at McNemar z = 2.66, which is a real effect
# and not noise.
#
# It still does not ship, because it does not generalise: on the five held-out
# crates the same setting scores -0.4 points, net -3 questions, z = 0.36. The
# tuning gain was three crates' worth of overfitting, which is exactly what a
# held-out set is for. Curiously the `test-name` generator gains in both
# (+15 and +13) while `doc-split` swings from +4 to -18 -- so there may be
# something real here for a future attempt with a sharper instrument.
STEM_LEVEL = 0        # 0 off, 1 plurals, 2 also -ing/-ed
W_NAME = 1.0          # a word in the item's own name
W_PATH = 1.0          # ...in its file path
W_SIG = 1.0           # ...in its signature
W_DOC = 1.0           # ...in its doc comment
W_MODDOC = 0.5        # ...in the file's module header, inherited
IDF_POW = 2.0
TF_SAT = True         # 1 + log(tf) rather than raw tf
AGG_TOPK = 1          # symbols averaged when rolling a score up to a file
AGG_NORM = 1.0        # 1.0 mean, 0.5 favours files that match repeatedly

# Below this share of public items documented, say so before answering. With an
# item's doc removed from the index, the chance of finding it from a
# description drops to 1% -- names and signatures carry almost none of the
# words a person types.
THIN_DOCS = 0.5

STOP = {
    'the', 'a', 'an', 'of', 'to', 'in', 'is', 'this', 'that', 'and', 'for',
    'it', 'with', 'as', 'be', 'are', 'or', 'if', 'on', 'from', 'by', 'not',
    'you', 'can', 'will', 'which', 'when', 'was', 'has', 'have', 'its', 'use',
    'returns', 'return', 'value', 'values', 'self', 'let', 'how', 'where',
    'what', 'does', 'get', 'set', 'make', 'want', 'need', 'rust',
}


def stem(word):
    """Fold the endings that separate how code is written from how it is asked
    about. A doc header says "Decoding and Encoding of PNG Images" and the
    question says "how is a png decoded" -- without this those share only the
    word png. Conservative on purpose: `decoder` keeps its `er`, because a
    decoder is a thing and decoding is an act, and collapsing them costs more
    than it pays."""
    if STEM_LEVEL <= 0 or len(word) < 5:
        return word
    for suf, rep in (('ies', 'y'), ('sses', 'ss'), ('es', ''), ('s', '')):
        if word.endswith(suf) and len(word) - len(suf) >= 3:
            return word[:len(word) - len(suf)] + rep
    if STEM_LEVEL >= 2:
        for suf in ('ing', 'ed'):
            if word.endswith(suf) and len(word) - len(suf) >= 4:
                return word[:len(word) - len(suf)]
    return word


def tokens(text):
    """Split on punctuation and camelCase both, so `parse_rfc3339` and
    `ParseError` and "parse an rfc 3339 timestamp" meet in the middle."""
    out = []
    for part in SPLIT.split(text or ''):
        if not part:
            continue
        for piece in CAMEL.split(part):
            p = piece.lower()
            if len(p) > 2 and p not in STOP:
                out.append(stem(p))
    return out


def sym_fields(s, doc=None):
    """Each field weighted separately. A word in the item's own name is much
    stronger evidence than the same word buried in a paragraph of prose about
    it, and treating them alike is the default this started with."""
    doc = s['doc'] if doc is None else doc
    return (
        (tokens(s['name']), W_NAME),
        (tokens(s['file']), W_PATH),
        (tokens(s['sig']) + [s['kind']], W_SIG),
        (tokens(doc), W_DOC),
    )


def build_bag(s, doc=None):
    bag = {}
    for toks, weight in sym_fields(s, doc):
        if weight <= 0:
            continue          # a zeroed field contributes nothing, not a zero
        for w in toks:
            bag[w] = bag.get(w, 0) + weight
    return bag


def sym_text(s, with_doc=True):
    t = tokens(s['name']) + tokens(s['file']) + tokens(s['sig'])
    t.append(s['kind'])
    if with_doc:
        t += tokens(s['doc'])
    return t


class Index(object):
    def __init__(self, path):
        self.path = path

        def rd(n):
            p = os.path.join(path, n)
            if not os.path.exists(p):
                return None
            return json.loads(open(p, encoding='utf-8').read())

        self.symbols = rd('symbols.json') or []
        self.edges = rd('edges.json') or []
        self.modules = rd('modules.json') or {}
        self.meta = rd('meta.json') or {}
        self.tests = rd('tests.json') or []
        self.module_examples = rd('module-examples.json') or {}
        if not self.symbols:
            raise SystemExit('no index at %s -- run repograph/index.py first'
                                % path)
        self._build()

    def _build(self):
        self.bags = [build_bag(s) for s in self.symbols]
        df = {}
        for bag in self.bags:
            for w in bag:
                df[w] = df.get(w, 0) + 1
        n = max(len(self.bags), 2)
        self.idf = {w: math.log(n / (1 + c)) / math.log(n)
                    for w, c in df.items()}
        # A module doc describes the whole file, so let every symbol in that
        # file inherit its words. Without this, "decoding and encoding of png
        # images" at the top of png.rs helps nothing inside it.
        self.moddoc = {}
        for f, md in self.modules.items():
            if W_MODDOC <= 0:
                continue
            b = {}
            for w in tokens(md):
                b[w] = b.get(w, 0) + W_MODDOC
            self.moddoc[f] = b
        for i, s in enumerate(self.symbols):
            for w, v in (self.moddoc.get(s['file']) or {}).items():
                self.bags[i][w] = self.bags[i].get(w, 0) + v
        self.centrality = {}
        for si, ti, _tok in self.edges:
            self.centrality.setdefault(ti, set()).add(self.symbols[si]['file'])
        self.centrality = {k: len(v) for k, v in self.centrality.items()}

    def score(self, qt, prior=None, hold_file=None, hold_doc=None,
                hold_rest=None):
        """Rarity-weighted term match, optionally nudged by how many files
        reference the symbol. The prior only ever re-orders things the text
        already matched; it can never introduce a symbol scoring zero, which is
        the line between a prior and the graph expansion that did not work.

        hold_file / hold_doc exist for the eval harness. A question generated
        from a file's module doc, asked against an index containing that module
        doc, is an identity lookup dressed as retrieval -- it was measured at
        66% that way against 1% with the text fully removed, and neither number
        means anything. Holding out exactly the generated text is what makes a
        generated question a question."""
        w = PRIOR_WEIGHT if prior is None else prior
        # Replace the file's module-doc words with whatever the question left
        # behind, rather than deleting them. Deleting strips the text from
        # every symbol in the file at once, which is the total-holdout case
        # that scores 1% and measures nothing -- it dragged module-doc
        # questions to 11% before this.
        held, keep = {}, {}
        if hold_file:
            held = self.moddoc.get(hold_file) or {}
            if hold_rest:
                for word in tokens(hold_rest):
                    keep[word] = keep.get(word, 0) + W_MODDOC
        out = []
        for i, bag in enumerate(self.bags):
            sym = self.symbols[i]
            # Tests are not answers. Someone asking where dates are added means
            # the implementation, not `test_date_add_assign` beside it. Nor are
            # benchmarks, examples or integration tests: they are in the
            # repository but they are not the thing being asked about. `sym`
            # and `file` still reach them.
            if sym.get('test') or sym.get('role', 'lib') != 'lib':
                continue
            if held and sym['file'] == hold_file:
                bag = dict(bag)
                for k, v in held.items():
                    if bag.get(k, 0) - v <= 0:
                        bag.pop(k, None)
                    else:
                        bag[k] -= v
                for k, v in keep.items():
                    bag[k] = bag.get(k, 0) + v
            if hold_doc is not None and i == hold_doc[0]:
                bag = build_bag(sym, doc=hold_doc[1])
                for k, v in (self.moddoc.get(sym['file']) or {}).items():
                    bag[k] = bag.get(k, 0) + v
            s = 0.0
            for t in qt:
                v = bag.get(t, 0)
                if v > 0:
                    tf = (1.0 + math.log(v)) if TF_SAT else v
                    s += tf * (self.idf.get(t, 1.0) ** IDF_POW)
            if s <= 0:
                continue
            if w > 0:
                s += (math.log10(self.centrality.get(i, 0) + 1) ** 2) * w
            out.append((s, i))
        out.sort(key=lambda x: -x[0])
        return out

    def by_file(self, scored):
        """Roll symbol scores up to files.

        `max` alone rewards size: a 3,000-line module gets far more chances to
        contain one strongly-matching symbol than a focused 80-line one, and
        the question was about the topic, not the volume. AGG_TOPK averages the
        best few instead, so a file has to be about the subject in several
        places rather than once.
        """
        groups = {}
        for s, i in scored:
            groups.setdefault(self.symbols[i]['file'], []).append((s, i))
        rows = []
        for f, items in groups.items():
            items.sort(key=lambda x: -x[0])
            top = items[0]
            if AGG_TOPK <= 1:
                score = top[0]
            else:
                take = items[:AGG_TOPK]
                score = sum(x[0] for x in take) / (len(take) ** AGG_NORM)
            rows.append((f, score, top[1]))
        rows.sort(key=lambda x: -x[1])
        return rows

    def vocab(self, key):
        """What this language calls things. Saying "crate" at someone reading
        a Go index is a small wrongness that recurs on every screen."""
        return language(self.meta.get('lang', 'rust')).VOCAB.get(key, key)

    def coverage_note(self):
        cov = self.meta.get('coverage') or {}
        ratio = cov.get('doc_ratio', 0.0)
        return ratio, cov


def confidence(idx, qt, scored):
    """What share of the question's words the index matched. NOT a prediction
    that the answer is right, and it was called CONFIDENCE until that claim was
    measured on 100 blind questions:

        matched a lot   7 questions, top-1 right 14%
        matched a little 92 questions, top-1 right 29%

    High word-overlap correlates with being *wrong*, because the way to match
    many words at once is to hit repeated boilerplate prose -- three flate2
    decoders share a stock sentence about trailing data, so any question about
    trailing data matches all three strongly and answers none of them. A label
    reading "CONFIDENCE high" on that is worse than no label, so the number is
    still shown, under a name that says what it is."""
    if not scored:
        return 0.0, False, list(qt)
    top_bag = idx.bags[scored[0][1]]
    covered = sum(1 for t in qt if t in top_bag)
    # Which words the whole index has never heard of. This is the actionable
    # half: a percentage tells a reader nothing they can act on, while "no
    # entry anywhere for: tty, terminal" tells them exactly which word to drop
    # or replace. tracing-subscriber has no terminal detection at all, and an
    # agent asking about it spent calls discovering that.
    unknown = [t for t in qt if t not in idx.idf]
    share = covered / max(len(qt), 1)
    return share, share >= 0.6, unknown


def cmd_ask(idx, question, prior=None):
    qt = tokens(question)
    if not qt:
        print('nothing to search for in %r' % question)
        return 1
    scored = idx.score(qt, prior)
    if not scored:
        print('NO MATCH for %r' % question)
        print('  Nothing in this index carries those words. Try the repo\'s own')
        print('  vocabulary -- `coverage` shows how much there is to search.')
        return 1

    share, conf, unknown = confidence(idx, qt, scored)
    ratio, cov = idx.coverage_note()
    print('QUESTION  %s' % question)
    print('=' * 68)
    if unknown:
        print('NOT IN THIS INDEX  %s' % ', '.join(unknown))
        print('  Those words appear nowhere in the repository. Replace them'
                ' or drop them.')
    if ratio < THIN_DOCS:
        print('  THIN DOCS: only %.0f%% of %s here carry documentation.'
                % (100 * ratio, cov.get('basis', 'public symbols')))
        print('  Retrieval works off documentation, so these results are weak')
        print('  for a reason that is not fixable by asking differently.')
    print()
    print('FILES')
    for f, s, i in idx.by_file(scored)[:3]:
        sym = idx.symbols[i]
        print('  %-44s %s:%d' % (f, f, sym['line']))
        if idx.modules.get(f):
            print('      %s' % idx.modules[f][:100])
    print()
    print('SYMBOLS')
    for s, i in scored[:5]:
        sym = idx.symbols[i]
        print('  %-28s %-8s %s:%d' % (sym['name'][:28], sym['kind'],
                                        sym['file'], sym['line']))
        if sym['doc']:
            print('      %s' % sym['doc'][:96])
    print()
    print('Read all three files, not just the first: measured 28% top-1 '
            'against 54% top-3 on blind questions.')
    return 0


def read_span(idx, sym, max_lines):
    """Pull a symbol's actual source. The index deliberately does not store
    bodies -- they are large and only ever needed here -- so this reads the
    file, and shrugs if the checkout has moved since indexing."""
    root = idx.meta.get('root')
    if not root:
        return None
    path = os.path.join(root, sym['file'])
    try:
        lines = open(path, encoding='utf-8', errors='replace').read().split('\n')
    except OSError:
        return None
    body = lines[sym['line'] - 1:sym['end']]
    if len(body) > max_lines:
        body = body[:max_lines] + ['    // ... %d more lines'
            % (sym['end'] - sym['line'] + 1 - max_lines)]
    return body


def cmd_context(idx, question, nfiles=3, source=False, budget=12000):
    """Everything needed to answer the question, in one call.

    `ask` ranks; this hands over the material. The split matters because of a
    number: on questions written blind, the first answer is right 28% of the
    time and the right answer is in the top three 54% of the time. A person
    reading a ranked list feels that 26-point gap as a wrong answer. A reader
    who gets all three candidates with their documentation attached just picks
    the right one, which turns the top-three figure into the accuracy that
    actually applies.

    So this prints candidates, plural, and never pretends to have decided.

    Kept deliberately small. Its job is to let a reader choose between three
    files, not to reproduce them -- an earlier version showed six symbols per
    candidate with long doc extracts, and four batched questions produced 47KB
    that a test agent had to spill to a file and read back. Three symbols and a
    short doc are enough to choose; `--source` and `--budget` are there for
    when they are not.
    """
    qt = tokens(question)
    if not qt:
        print('nothing to search for in %r' % question)
        return 1
    scored = idx.score(qt)
    if not scored:
        print('NO MATCH for %r' % question)
        return 1

    share, conf, unknown = confidence(idx, qt, scored)
    ratio, cov = idx.coverage_note()
    per_file = {}
    for sc, i in scored:
        per_file.setdefault(idx.symbols[i]['file'], []).append((sc, i))
    order = [f for f, _s, _i in idx.by_file(scored)][:nfiles]

    out = []
    out.append('QUESTION  %s' % question)
    out.append('REPO      %s' % (idx.meta.get('name') or '?'))
    if unknown:
        out.append('NOT IN THIS INDEX  %s' % ', '.join(unknown))
        out.append('  Those words appear nowhere in the repository -- replace'
            ' or drop them.')
    if ratio < THIN_DOCS:
        out.append('WARNING   only %.0f%% of %s here are documented; retrieval '
            'runs on documentation, so these candidates are weak.'
            % (100 * ratio, cov.get('basis', 'public symbols')))
    out.append('')
    out.append('%d candidates, most likely first. The right answer is in the '
        'top three about 54%% of the time and is first about 28%%, so read all '
        'of them before deciding, and cite the file:line you actually used.'
        % len(order))

    # Each candidate gets its own slice of the budget, rather than the whole
    # output being cut at the end. Truncating the joined text deletes the last
    # candidate entirely and silently -- and since the third candidate is a
    # large part of why this command returns three, losing it defeats the
    # command. Better a shorter view of all three than a full view of two.
    blocks = []
    for rank, f in enumerate(order, 1):
        blk = ['', '=' * 70, 'CANDIDATE %d  %s' % (rank, f)]
        md = idx.modules.get(f)
        if md:
            blk.append('  module doc: %s' % md[:200])
        picks = sorted(per_file.get(f, []), key=lambda x: -x[0])[:3]
        for sc, i in picks:
            sym = idx.symbols[i]
            blk.append('')
            blk.append('  %s  %s  (%s:%d)'
                % (sym['kind'], sym['name'], sym['file'], sym['line']))
            blk.append('      %s' % sym['sig'][:160])
            # One line, not the code itself -- context is for choosing between
            # candidates, and it was cut to a third of its size for exactly
            # that reason. Say the verified code exists and how to fetch it.
            runnable = [e for e in (sym.get('examples') or [])
                if e.get('kind') == 'runs']
            if runnable:
                blk.append('      has %d verified example%s -- `example %s`'
                    % (len(runnable), '' if len(runnable) == 1 else 's',
                        sym['name']))
            if sym['doc']:
                for chunk in wrap(sym['doc'], 200)[:2]:
                    blk.append('      %s' % chunk)
            if source:
                body = read_span(idx, sym, 40)
                if body:
                    blk.append('      ---')
                    for ln in body:
                        blk.append('      %s' % ln[:160])
        blocks.append(blk)

    head = '\n'.join(out)
    share = max((budget - len(head)) // max(len(blocks), 1), 400)
    parts = [head]
    for blk in blocks:
        body = '\n'.join(blk)
        if len(body) > share:
            body = body[:share] + ('\n      [candidate truncated; raise '
                '--budget to see the rest]')
        parts.append(body)
    print('\n'.join(parts))
    return 0


def wrap(text, width):
    words, line, out = text.split(), '', []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = (line + ' ' + w).strip()
    if line:
        out.append(line)
    return out[:6]


def language(name):
    """The plugin for an index's language. Loaded through the same contract
    index.py uses, so the query surface never asks which language it has."""
    if name == 'go':
        import lang_go
        return lang_go
    import lang_rust
    return lang_rust


def show_example(e, indent='    ', lang='rust'):
    """Print one example with an honest label.

    `runs` is the only bucket a reader may copy. `should_panic` and
    `compile_fail` compile and pass their doc test while demonstrating a
    crash, so a flag meaning "cargo verified this" would hand over a panic as
    working code."""
    # Both the wording and the caveat come from the language plugin. The
    # printer does not know which language it has, which is the point: a
    # language with no verified examples at all declares that in its
    # VERIFICATION table rather than needing a branch here.
    spec = language(lang)
    label, _copyable = spec.VERIFICATION.get(
        e.get('kind'), ('UNKNOWN', False))
    print('%s[%s]' % (indent, label))
    gate = e.get('gate')
    if gate:
        kind, value = gate
        if kind == 'feature':
            print('%sneeds feature "%s" enabled in %s, or it will not build'
                % (indent, value, spec.VOCAB.get('manifest', 'the manifest')))
        else:
            # A build tag is a platform, not a switch. Telling someone to
            # "enable !windows in go.mod" would be nonsense, which is why the
            # kind travels with the value.
            print('%sonly builds where the constraint `%s` holds'
                % (indent, value))
    if e.get('requires_feature'):
        # The kind travels with the value because the advice differs: a
        # cargo feature is something you switch on, a build tag is a platform
        # you either are or are not on.
        print('%sneeds feature "%s" enabled in %s, or it will not build'
            % (indent, e['requires_feature'], spec.VOCAB.get('manifest', '')))
    caveat = spec.COPY_CAVEAT
    if e.get('kind') == 'runs' and caveat:
        # Rust's doc tests run inside the crate with every feature and
        # dev-dependency available and a reader copying the code has neither;
        # measured, 9 of 10 extracted examples compiled once their
        # prerequisites existed. Go has no feature flags, so its plugin
        # declares no caveat and none is printed.
        print('%s(%s)' % (indent, caveat))
    for ln in e['code'].split('\n'):
        print('%s%s' % (indent, ln))


def cmd_start(idx):
    """How do I use this crate, answered with the author's own verified code.

    No ranking is involved, which is why this is worth having: the crate author
    already chose what to lead with in `lib.rs`, and the doc tests already
    compiled it. Where an author wrote no module header -- regex, url and
    clap_builder among the crates tested -- fall back to the examples on the
    most widely referenced public symbols, which is a fixed property of the
    crate rather than an answer to a query.
    """
    name = idx.meta.get('name', '?')
    print('%s -- getting started' % name)
    print('=' * 68)

    lead = []
    for path in ('src/lib.rs', 'lib.rs', 'src/main.rs'):
        if idx.module_examples.get(path):
            lead = [(path, e) for e in idx.module_examples[path]]
            break
    if not lead:
        for path, exs in sorted(idx.module_examples.items()):
            lead.extend((path, e) for e in exs)
            if len(lead) >= 3:
                break

    if lead:
        # Runnable code first. A crate header often opens with a JSON blob or a
        # shell snippet to set the scene, and leading with an unverified
        # fragment squanders the one advantage this command has.
        order = {'runs': 0, 'compiles': 1, 'unverified': 2, 'failure': 3}
        lead.sort(key=lambda pe: order.get(pe[1].get('kind'), 9))
        print('From the %s\'s own documentation header:' % idx.vocab('unit'))
        for path, e in lead[:3]:
            print()
            print('  (%s)' % path)
            show_example(e, indent='  ', lang=idx.meta.get('lang', 'rust'))
        return 0

    print('This %s has no module-level documentation header, so there is'
        % idx.vocab('unit'))
    print('no author-written introduction. Falling back to the examples on its')
    print('most widely used public items.')
    ranked = sorted(
        ((idx.centrality.get(i, 0), i) for i, sym in enumerate(idx.symbols)
            if sym.get('public') and sym.get('examples')
            and sym.get('role', 'lib') == 'lib'),
        key=lambda x: -x[0])
    if not ranked:
        print()
        print('  ...and it has none of those either. Nothing to show.')
        return 1
    for used, i in ranked[:3]:
        sym = idx.symbols[i]
        print()
        print('  %s %s  (%s:%d)  referenced by %d files'
            % (sym['kind'], sym['name'], sym['file'], sym['line'], used))
        for e in sym['examples'][:1]:
            if sym.get('requires_feature'):
                e = dict(e, requires_feature=sym['requires_feature'])
            show_example(e, indent='  ', lang=idx.meta.get('lang', 'rust'))
    return 0


def cmd_example(idx, name):
    """Working code for a symbol, taken from its doc comment.

    This is the one thing here that is not probabilistic. Retrieval has no
    measured advantage over grep, but a doc example marked `runs` was compiled
    and executed by the crate's own test suite, so it is correct by
    construction. The failure mode is that there is no example, never that the
    example is wrong.
    """
    hits = [s for s in idx.symbols if s['name'].lower() == name.lower()
        and s.get('examples')]
    if not hits:
        near = [s for s in idx.symbols
            if name.lower() in s['name'].lower() and s.get('examples')]
        if not near:
            any_name = [s for s in idx.symbols
                if s['name'].lower() == name.lower()]
            if any_name:
                print('%s exists (%s:%d) but its documentation carries no'
                    ' example.' % (name, any_name[0]['file'],
                        any_name[0]['line']))
                return 1
            print('no symbol named %r' % name)
            return 1
        hits = near[:3]

    for s in hits[:3]:
        print('%s  %s  (%s:%d)' % (s['kind'], s['name'], s['file'], s['line']))
        if s['doc']:
            print('    %s' % s['doc'][:200])
        spec = language(idx.meta.get('lang', 'rust'))
        for e in s['examples']:
            # The gate lives on the symbol, not the example: in Rust it is the
            # enclosing module's `#[cfg(feature)]`, in Go the file's build tag.
            g = spec.gate_of(s)
            if g:
                e = dict(e, gate=g)
            print()
            show_example(e, lang=idx.meta.get('lang', 'rust'))
        print()
    return 0


def cmd_sym(idx, name):
    hits = [s for s in idx.symbols if s['name'].lower() == name.lower()]
    if not hits:
        hits = [s for s in idx.symbols if name.lower() in s['name'].lower()][:8]
    if not hits:
        print('no symbol matching %r' % name)
        return 1
    for s in hits[:8]:
        print(json.dumps(s, indent=4))
    return 0


def cmd_file(idx, path, limit=40):
    """List a directory, or the contents of a file.

    Never silently shortens either. An earlier version printed the first three
    matching files with no marker, so asking for `src` in a 49-file crate
    showed three of them and looked complete -- an agent reading that concluded
    the gzip writer did not exist. A wrong ranking is at least visible; a
    missing entry is not, so anything dropped here says so.
    """
    syms = [s for s in idx.symbols if path.lower() in s['file'].lower()]
    if not syms:
        print('no file matching %r' % path)
        return 1
    files = sorted({s['file'] for s in syms})

    # A path matching many files is a browse, not a read. Show every file --
    # that is the question being asked -- rather than dumping the symbols of an
    # arbitrary few.
    if len(files) > 3:
        print('%d files match %r' % (len(files), path))
        for f in files:
            n = sum(1 for s in syms if s['file'] == f)
            md = idx.modules.get(f) or ''
            print('  %-52s %3d symbols  %s' % (f, n, md[:60]))
        print()
        print('Name one of them to see inside it.')
        return 0

    for f in files:
        inside = [x for x in syms if x['file'] == f]
        print('%s  (%d symbols)' % (f, len(inside)))
        if idx.modules.get(f):
            print('  %s' % idx.modules[f][:160])
        for s in inside[:limit]:
            flag = ' [test]' if s.get('test') else ''
            print('  %5d  %-8s %s%s' % (s['line'], s['kind'], s['name'], flag))
        if len(inside) > limit:
            print('  ... %d more not shown; raise with `file <path> --all`'
                % (len(inside) - limit))
        print()
    return 0


def cmd_callers(idx, name):
    targets = {i for i, s in enumerate(idx.symbols)
                if s['name'].lower() == name.lower()}
    if not targets:
        print('no symbol named %r' % name)
        return 1
    seen = {}
    for si, ti, tok in idx.edges:
        if ti in targets:
            s = idx.symbols[si]
            seen.setdefault((s['file'], s['name']), s['line'])
    if not seen:
        print('%s: nothing in this %s references it'
            % (name, idx.vocab('unit')))
        return 0
    print('%s is referenced by %d symbols' % (name, len(seen)))
    for (f, n), line in sorted(seen.items())[:30]:
        print('  %-30s %s:%d' % (n[:30], f, line))
    return 0


def cmd_coverage(idx):
    ratio, cov = idx.coverage_note()
    print('%s' % idx.meta.get('name', '?'))
    print('=' * 68)
    print('  files                     %d' % cov.get('files', 0))
    print('  symbols                   %d' % cov.get('symbols', 0))
    print('  public symbols            %d' % cov.get('public_symbols', 0))
    print('  documented (%-11s) %d/%d (%.0f%%)'
            % (cov.get('basis', 'public'), cov.get('documented_of_basis', 0),
                cov.get('basis_size', 0), 100 * ratio))
    ex = [e for sym in idx.symbols for e in (sym.get('examples') or [])]
    runnable = [e for e in ex if e.get('kind') == 'runs']
    print('  doc examples              %d (%d verified by %s)'
            % (len(ex), len(runnable), idx.vocab('verifier')))
    print('  files with a module doc   %d (%.0f%%)'
            % (cov.get('files_with_module_doc', 0),
                100 * cov.get('module_doc_ratio', 0)))
    print()
    if ratio >= 0.7:
        print('  WELL DOCUMENTED overall. Description-style questions should')
        print('  work -- but read the breakdown below before believing that.')
    elif ratio >= THIN_DOCS:
        print('  PARTLY DOCUMENTED. Expect gaps in undocumented corners.')
    else:
        print('  THIN. Retrieval here runs on documentation, and with an')
        print('  item\'s doc removed the chance of finding it from a')
        print('  description of it falls to 1%. Answers will be weak, and no')
        print('  amount of rephrasing fixes it -- the text is not there.')

    # A crate-wide average hides exactly what a reader needs. tracing-subscriber
    # reports 94% documented while its filter internals carry almost no doc
    # comments, and the filter internals are precisely where retrieval failed
    # in testing. The average was true and useless; the breakdown is what says
    # "searching will not work *here*".
    # Counted over EVERY library symbol, not just the public ones, because
    # that is what retrieval searches. The distinction is the whole point here:
    # tracing-subscriber documents 94% of its public API and 18% of what is
    # actually in src/filter, and the filter internals are where a test agent's
    # questions failed. `src/filter/directive.rs` documents 3 of 42 symbols
    # while exporting one documented public item, so a public-API measure calls
    # it perfect and a searcher finds nothing.
    by_dir = {}
    for s in idx.symbols:
        if s.get('role', 'lib') != 'lib':
            continue
        d = s['file'].rsplit('/', 1)[0] if '/' in s['file'] else '.'
        cell = by_dir.setdefault(d, [0, 0])
        cell[0] += 1 if s.get('doc') else 0
        cell[1] += 1
    rows = [(d, a, b) for d, (a, b) in by_dir.items() if b >= 5]
    if not rows:
        return 0
    rows.sort(key=lambda r: (r[1] / r[2], -r[2]))
    print()
    print('  BY DIRECTORY -- every symbol, documented or not, worst first.')
    print('  This is what retrieval searches, so it predicts where asking')
    print('  questions works and where it does not.')
    for d, a, b in rows[:12]:
        share = a / b
        flag = '  <-- ask here and expect misses' if share < 0.25 else ''
        print('    %-42s %4d/%-5d %3.0f%%%s' % (d, a, b, 100 * share, flag))
    thin = [r for r in rows if r[1] / r[2] < 0.25]
    if thin:
        print()
        print('  %d of %d directories fall below 25%%. Navigate those with'
            % (len(thin), len(rows)))
        print('  `file` and `callers` rather than by asking about them --')
        print('  the words you would search for are not written down there.')
    return 0


def cmd_stats(idx):
    kinds = {}
    for s in idx.symbols:
        kinds[s['kind']] = kinds.get(s['kind'], 0) + 1
    print('%s  %s' % (idx.meta.get('name', '?'), idx.meta.get('root', '')))
    print('  symbols  %d' % len(idx.symbols))
    for k, v in sorted(kinds.items(), key=lambda x: -x[1]):
        print('      %-12s %d' % (k, v))
    print('  edges    %d' % len(idx.edges))
    print('  tests    %d' % len(idx.tests))
    print('  module docs %d' % len(idx.modules))
    print('  prior    %s' % ('OFF' if PRIOR_WEIGHT <= 0 else PRIOR_WEIGHT))
    return 0


def resolve_hold(idx, hold):
    """Turn a question's `hold` block into arguments `score` understands."""
    if not hold:
        return None, None, None
    f = hold.get('file')
    if hold.get('line') is not None:
        want = (f, hold['line'])
        for i, s in enumerate(idx.symbols):
            if (s['file'], s['line']) == want:
                return None, (i, hold.get('rest', '')), None
    return f, None, hold.get('rest')


def load_questions(path):
    """Accepts the generated form and the hand-written form both."""
    spec = json.loads(open(path, encoding='utf-8').read())
    qs = []
    if isinstance(spec, dict) and isinstance(spec.get('questions'), list):
        for row in spec['questions']:
            qs.append((row['q'], row['ok'], row.get('src', 'hand'),
                        row.get('hold')))
        return qs
    groups = spec if isinstance(spec, dict) else {'all': spec}
    for _k, v in groups.items():
        if not isinstance(v, list):
            continue
        for row in v:
            qs.append((row[0], row[1], 'hand', None))
    return qs


def run_questions(idx, questions, prior=None):
    t1 = t3 = 0
    rows = []
    for q, frags, src, hold in questions:
        hf, hd, hr = resolve_hold(idx, hold)
        scored = idx.score(tokens(q), prior, hold_file=hf, hold_doc=hd,
                            hold_rest=hr)
        files = [f for f, _s, _i in idx.by_file(scored)]
        pos = next((r for r, f in enumerate(files)
                    if any(fr.lower() in f.lower() for fr in frags)), None)
        t1 += pos == 0
        t3 += pos is not None and pos < 3
        rows.append((q, files[0] if files else 'NO MATCH', pos, src))
    return t1, t3, rows


def cmd_eval(idx, path, prior=None):
    if not os.path.exists(path):
        print('no such question set: %s' % path)
        return 1
    questions = load_questions(path)
    if not questions:
        print('no questions in %s' % path)
        return 1
    t1, t3, rows = run_questions(idx, questions, prior)
    n = len(questions)
    print('EVAL  %s  (%d questions)' % (os.path.basename(path), n))
    print('=' * 68)
    if n <= 40:
        for q, f, pos, _src in rows:
            mark = '1' if pos == 0 else (str(pos + 1) if pos is not None else 'X')
            print('  [%s] %-42s -> %s' % (mark, q[:42], f))
        print()
    print('  top1 %d/%d (%.0f%%)   top3 %d/%d (%.0f%%)'
            % (t1, n, 100.0 * t1 / n, t3, n, 100.0 * t3 / n))
    by = {}
    for _q, _f, pos, src in rows:
        d = by.setdefault(src, [0, 0, 0])
        d[0] += pos == 0
        d[1] += pos is not None and pos < 3
        d[2] += 1
    if len(by) > 1:
        print()
        print('  by provenance -- these have different biases, so do not pool')
        print('  them when judging a mechanism:')
        for src, (a, b, c) in sorted(by.items()):
            print('      %-12s top1 %4d/%-4d (%3.0f%%)   top3 %4d/%-4d (%3.0f%%)'
                    % (src, a, c, 100.0 * a / c, b, c, 100.0 * b / c))
    print()
    print('  A measurement, not a gate. Generated sets are for tuning;')
    print('  hand-written ones are for judging, and reading one retires it.')
    return 0


def cmd_selftest(idx):
    """Behaviour the query surface must keep, independent of any one repo."""
    fails = []

    qt = tokens('how is a PNG decoded')
    if 'png' not in qt:
        fails.append('tokens: lost png from a natural question')
    if 'how' in qt or 'the' in qt:
        fails.append('tokens: kept a stop word')
    if 'parse' not in tokens('ParseError'):
        fails.append('tokens: did not split camelCase')

    scored = idx.score(tokens('zzzznotarealword'))
    if scored:
        fails.append('score: matched a nonsense token')

    top = idx.score(tokens(list(idx.symbols)[0]['name']))
    if not top:
        fails.append('score: a real symbol name matched nothing')

    files = idx.by_file(idx.score(tokens('error')))
    if files and len({f for f, _s, _i in files}) != len(files):
        fails.append('by_file: returned a file twice')

    ratio, _cov = idx.coverage_note()
    if not 0.0 <= ratio <= 1.0:
        fails.append('coverage: doc_ratio outside 0..1')

    # The example feature's whole claim is that the code is known good, so
    # these three guard the claim rather than the plumbing.
    examples = [(sym, e) for sym in idx.symbols
        for e in (sym.get('examples') or [])]
    for sym, e in examples:
        if e.get('kind') not in ('runs', 'compiles', 'unverified', 'failure'):
            fails.append('example %s: no verification bucket' % sym['name'])
            break
    for sym, e in examples:
        marker = [ln for ln in e['code'].split(chr(10))
            if ln.lstrip().startswith('# ')]
        if marker:
            fails.append('example %s: raw doc-test hidden marker survived'
                % sym['name'])
            break
    # `start` must produce something on any crate: the author's header if
    # there is one, and the most-referenced documented items if there is not.
    buf4, real4 = io.StringIO(), sys.stdout
    sys.stdout = buf4
    try:
        cmd_start(idx)
    finally:
        sys.stdout = real4
    started = buf4.getvalue()
    if 'getting started' not in started:
        fails.append('start: produced no header')
    if examples and 'Nothing to show' in started:
        fails.append('start: gave up although examples exist')

    for sym, e in examples:
        if e.get('kind') == 'failure':
            buf3, real3 = io.StringIO(), sys.stdout
            sys.stdout = buf3
            try:
                show_example(e, lang=idx.meta.get('lang', 'rust'))
            finally:
                sys.stdout = real3
            if 'DEMONSTRATES FAILURE' not in buf3.getvalue():
                fails.append('example %s: failure case shown without its label'
                    % sym['name'])
            break

    # context is what an agent reads, so its shape is load-bearing: three
    # candidates, each citable, and no stray %% from a format string.
    buf, real = io.StringIO(), sys.stdout
    sys.stdout = buf
    probe = idx.symbols[0]['name']
    for cand in idx.symbols:
        if cand.get('role', 'lib') != 'lib':
            continue
        words = [w for w in tokens(cand.get('doc') or '') if len(w) > 3]
        if len(words) >= 3:
            probe = ' '.join(words[:6])
            break
    try:
        cmd_context(idx, probe, nfiles=3)
    finally:
        sys.stdout = real
    got = buf.getvalue()
    if '%%' in got:
        fails.append('context: unformatted %% reached the output')
    if got.count('CANDIDATE ') < 1:
        fails.append('context: printed no candidates')

    # `file` on a directory must never quietly shorten its listing. It used to
    # print three of forty-nine matches with no marker, and an agent reading
    # that concluded a whole module did not exist.
    dirs = {}
    for sym in idx.symbols:
        head = sym['file'].rsplit('/', 1)[0]
        dirs.setdefault(head, set()).add(sym['file'])
    big = max(dirs.items(), key=lambda kv: len(kv[1]), default=(None, set()))
    if big[0] and len(big[1]) > 3:
        buf2, real2 = io.StringIO(), sys.stdout
        sys.stdout = buf2
        try:
            cmd_file(idx, big[0])
        finally:
            sys.stdout = real2
        listed = buf2.getvalue()
        missing = [f for f in big[1] if f not in listed]
        if missing:
            fails.append('file: listed %d of %d files in %s without saying so'
                % (len(big[1]) - len(missing), len(big[1]), big[0]))
    if 'QUESTION' not in got or 'CANDIDATE' not in got:
        fails.append('context: dropped its header')

    saved = globals()['PRIOR_WEIGHT']
    a = [i for _s, i in idx.score(tokens('decode an image'), 0.0)]
    b = [i for _s, i in idx.score(tokens('decode an image'), 2.0)]
    globals()['PRIOR_WEIGHT'] = saved
    if set(a) != set(b):
        fails.append('prior: changed which symbols match, not just their order')

    for f in fails:
        print('  FAIL %s' % f)
    print('selftest %s (%d checks)' % ('OK' if not fails else 'FAILED', 16))
    return 1 if fails else 0


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, ValueError):
            pass
    if len(argv) < 3 or argv[1] in ('-h', '--help', 'help'):
        print(__doc__)
        return 0
    prior = None
    if '--prior' in argv:
        k = argv.index('--prior')
        prior = float(argv[k + 1])
        argv = argv[:k] + argv[k + 2:]

    idxpath, cmd, rest = argv[1], argv[2], argv[3:]
    idx = Index(idxpath)
    joined = ' '.join(rest)
    if cmd == 'ask':
        return cmd_ask(idx, joined, prior)
    if cmd == 'context':
        nfiles, source, budget = 3, False, 4000
        rest2 = []
        k = 0
        while k < len(rest):
            a = rest[k]
            if a == '--source':
                source = True
            elif a == '--files' and k + 1 < len(rest):
                k += 1
                nfiles = max(1, int(rest[k]))
            elif a == '--budget' and k + 1 < len(rest):
                k += 1
                budget = max(500, int(rest[k]))
            else:
                rest2.append(a)
            k += 1
        return cmd_context(idx, ' '.join(rest2), nfiles, source, budget)
    if cmd == 'sym':
        return cmd_sym(idx, joined)
    if cmd == 'start':
        return cmd_start(idx)
    if cmd == 'example':
        return cmd_example(idx, joined)
    if cmd == 'file':
        want_all = '--all' in rest
        rest = [a for a in rest if a != '--all']
        return cmd_file(idx, ' '.join(rest), 100000 if want_all else 40)
    if cmd == 'callers':
        return cmd_callers(idx, joined)
    if cmd == 'coverage':
        return cmd_coverage(idx)
    if cmd == 'stats':
        return cmd_stats(idx)
    if cmd == 'eval':
        return cmd_eval(idx, joined, prior)
    if cmd == 'selftest':
        return cmd_selftest(idx)
    print(__doc__)
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
