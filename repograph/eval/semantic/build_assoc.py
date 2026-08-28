"""Learn term associations from doc comments across the local crate registry.

The question this exists to answer: a developer writes "colored output" and the
crate writes "ansi". Stemming cannot bridge that -- it folds morphology, not
lexemes -- and it is what the prose failures look like.

Trained with the evaluation crates held out, because a table that has read
chrono's docs would partly memorise chrono's vocabulary and the gain would not
transfer.
"""
import json
import math
import os
import random
import re
import sys
from collections import Counter, defaultdict

REG = ('C:/Users/Owen/.cargo/registry/src/'
        'index.crates.io-1949cf8c6b5b557f')

# Held out of training: everything an eval touches, plus the judgment repos.
HOLDOUT = ('chrono', 'serde_json', 'toml', 'url', 'tracing-subscriber',
            'flate2', 'regex', 'clap', 'clap_builder', 'rand', 'rayon',
            'indexmap', 'syn', 'image')

SPLIT = re.compile(r'[^A-Za-z0-9]+')
CAMEL = re.compile(r'(?<=[a-z0-9])(?=[A-Z])')
DOC = re.compile(r'^\s*//[/!](.*)$', re.M)

# Doc boilerplate. These co-occur with everything, and a magnet term becomes
# everyone's associate -- the same failure that inverted the confidence signal.
STOP = set('''the a an of to in is this that and for it with as be are or if on
from by not you can will which when was has have its use returns return value
values self let how where what does get set make want need rust example
examples panics errors safety note see also used using given into out new
create creates method function type struct enum trait impl fn let mut ref
some none ok err result option vec string str usize isize bool char
first second last next prev item items one two all any each other than then
must should would could may might there here their them they than because
however therefore thus so but and/or etc via per such same different
'''.split())


def tokens(text):
    out = []
    for part in SPLIT.split(text or ''):
        if not part:
            continue
        for piece in CAMEL.split(part):
            p = piece.lower()
            if 2 < len(p) < 24 and p not in STOP and not p.isdigit():
                out.append(p)
    return out


def crate_name(path):
    base = os.path.basename(path)
    return re.sub(r'-\d+\..*$', '', base)


def main():
    n_crates = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    out_path = sys.argv[2] if len(sys.argv) > 2 else None
    window = 8

    dirs = [d for d in (os.path.join(REG, x) for x in os.listdir(REG))
            if os.path.isdir(d)]
    dirs = [d for d in dirs if crate_name(d) not in HOLDOUT]
    random.seed(11)
    random.shuffle(dirs)
    dirs = dirs[:n_crates]

    df = Counter()            # in how many crates does a term appear
    pair = Counter()
    freq = Counter()
    used = 0
    for d in dirs:
        seen_here = set()
        blocks = 0
        for base, sub, files in os.walk(d):
            sub[:] = [x for x in sub
                        if x not in ('target', '.git', 'tests', 'benches')]
            for f in files:
                if not f.endswith('.rs'):
                    continue
                try:
                    text = open(os.path.join(base, f), encoding='utf-8',
                                errors='replace').read()
                except OSError:
                    continue
                for line in DOC.findall(text):
                    toks = tokens(line)
                    if not 2 <= len(toks) <= 40:
                        continue
                    blocks += 1
                    seen_here.update(toks)
                    for i, a in enumerate(toks):
                        freq[a] += 1
                        for b in toks[i + 1:i + 1 + window]:
                            if a != b:
                                pair[(a, b) if a < b else (b, a)] += 1
        if blocks:
            used += 1
            for t in seen_here:
                df[t] += 1

    total = max(sum(freq.values()), 1)
    npairs = max(sum(pair.values()), 1)

    # A term is only allowed associates if it is neither vanishingly rare nor
    # so common it means nothing. The df ceiling is what stops boilerplate
    # becoming everyone's neighbour.
    lo, hi = 3, int(0.25 * used)
    assoc = defaultdict(list)
    for (a, b), c in pair.items():
        if c < 6:
            continue
        if not (lo <= df[a] <= hi and lo <= df[b] <= hi):
            continue
        pmi = math.log((c / npairs) / ((freq[a] / total) * (freq[b] / total)))
        if pmi <= 0:
            continue
        assoc[a].append((pmi, b))
        assoc[b].append((pmi, a))

    table = {}
    for t, lst in assoc.items():
        lst.sort(reverse=True)
        table[t] = [w for _p, w in lst[:6]]

    print('trained on %d crates (%d held out by name)' % (used, len(HOLDOUT)))
    print('  vocabulary %d, pairs kept %d, terms with associates %d'
            % (len(freq), len(pair), len(table)))
    if out_path:
        payload = {'trained_on': used, 'holdout': list(HOLDOUT),
                    'assoc': table}
        open(out_path, 'w', encoding='utf-8', newline='\n').write(
            json.dumps(payload, indent=0) + '\n')
        print('  wrote %s (%.1f MB)'
                % (out_path, os.path.getsize(out_path) / 1e6))
    for probe in ('color', 'ansi', 'pipe', 'timezone', 'decode', 'parse',
                    'terminal'):
        if probe in table:
            print('    %-10s -> %s' % (probe, ', '.join(table[probe][:6])))
    return 0


if __name__ == '__main__':
    sys.exit(main())
