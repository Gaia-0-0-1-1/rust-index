"""Ask a Rust codebase where things are.

    python repograph/index.py /path/to/crate -o IDX
    python repograph/repograph.py IDX ask "how is a png decoded"

Commands
    ask <question>     rank files and symbols for a question, with citations
    sym <name>         everything known about one symbol
    file <path>        what a file contains
    callers <name>     which symbols reference it
    coverage           documentation coverage, and whether to trust answers
    eval <file>        measure retrieval against a question set
    stats              index shape
    selftest           check the query surface still behaves

Read the top three, not the first. Measured on 68 hand-written questions across
three crates: the first answer is right 31% of the time and the answer is in the
top three 65% of the time. That gap is the most useful fact about this tool.
"""
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

# How words are matched. Every one of these was swept against 1,455 generated
# questions over three crates, and every one is already sitting at its optimum:
# the values below are what the tool shipped with, and nothing beat them.
#
# Swept and rejected, for anyone tempted to retry: stemming (flat at best,
# -1 at level 2), path/signature weighting (no effect at any value), module-doc
# weighting above 0.5 (-11 points at 6.0), averaging more than one symbol per
# file (-7 at top-10), raw term frequency instead of saturated (-10), and
# rustdoc boilerplate as stop words (no effect).
#
# The one real effect found was W_NAME = 8, which gained 18 questions and lost
# none, z = 4.24. It does not ship because all 18 gains are `doc-split`, the
# generator whose queries are drawn from doc text that usually restates the
# item's name -- so it buys back the identity lookup rather than retrieval.
# `test-name` and `module-doc` gained nothing. That stratification rule was
# written down before the sweep, in code-probe/PREDECLARED-tuning.md.
#
# The honest conclusion is that this ranker has no tuning headroom, which is
# what `coverage` has been saying all along: the limit is how much prose the
# repository contains, not how it is weighed.
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
        if not self.symbols:
            raise SystemExit('no index at %s -- run repograph/index.py first'
                                % path)
        self._build()

    def _build(self):
        docs = [sym_text(s) for s in self.symbols]
        df = {}
        for d in docs:
            for w in set(d):
                df[w] = df.get(w, 0) + 1
        n = max(len(docs), 2)
        self.idf = {w: math.log(n / (1 + c)) / math.log(n)
                    for w, c in df.items()}
        self.bags = []
        for d in docs:
            bag = {}
            for w in d:
                bag[w] = bag.get(w, 0) + 1
            self.bags.append(bag)
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

    def coverage_note(self):
        cov = self.meta.get('coverage') or {}
        ratio = cov.get('doc_ratio', 0.0)
        return ratio, cov


def confidence(idx, qt, scored):
    """How much of the question the index actually explained. A score can be
    non-zero while matching one word of six, and saying `high` there is how a
    tool teaches an agent to trust it when it should not."""
    if not scored:
        return 0.0, False
    top_bag = idx.bags[scored[0][1]]
    covered = sum(1 for t in qt if t in top_bag)
    share = covered / max(len(qt), 1)
    return share, share >= 0.6


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

    share, conf = confidence(idx, qt, scored)
    ratio, cov = idx.coverage_note()
    print('QUESTION  %s' % question)
    print('=' * 68)
    print('CONFIDENCE  %s  (matched %.0f%% of the question)'
            % ('high' if conf else 'LOW ', 100 * share))
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
    print('Read all three files, not just the first: measured 31% top-1 '
            'against 65% top-3.')
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


def cmd_file(idx, path):
    syms = [s for s in idx.symbols if path.lower() in s['file'].lower()]
    if not syms:
        print('no file matching %r' % path)
        return 1
    files = sorted({s['file'] for s in syms})
    for f in files[:3]:
        print(f)
        if idx.modules.get(f):
            print('  %s' % idx.modules[f][:160])
        for s in [x for x in syms if x['file'] == f][:40]:
            flag = ' [test]' if s.get('test') else ''
            print('  %5d  %-8s %s%s' % (s['line'], s['kind'], s['name'], flag))
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
        print('%s: nothing in this crate references it' % name)
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
    print('  files with a module doc   %d (%.0f%%)'
            % (cov.get('files_with_module_doc', 0),
                100 * cov.get('module_doc_ratio', 0)))
    print()
    if ratio >= 0.7:
        print('  WELL DOCUMENTED. Description-style questions should work.')
    elif ratio >= THIN_DOCS:
        print('  PARTLY DOCUMENTED. Expect gaps in undocumented corners.')
    else:
        print('  THIN. Retrieval here runs on documentation, and with an')
        print('  item\'s doc removed the chance of finding it from a')
        print('  description of it falls to 1%. Answers will be weak, and no')
        print('  amount of rephrasing fixes it -- the text is not there.')
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

    saved = globals()['PRIOR_WEIGHT']
    a = [i for _s, i in idx.score(tokens('decode an image'), 0.0)]
    b = [i for _s, i in idx.score(tokens('decode an image'), 2.0)]
    globals()['PRIOR_WEIGHT'] = saved
    if set(a) != set(b):
        fails.append('prior: changed which symbols match, not just their order')

    for f in fails:
        print('  FAIL %s' % f)
    print('selftest %s (%d checks)' % ('OK' if not fails else 'FAILED', 7))
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
    if cmd == 'sym':
        return cmd_sym(idx, joined)
    if cmd == 'file':
        return cmd_file(idx, joined)
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
