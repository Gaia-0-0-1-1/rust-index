"""Ceiling and fan-out for semantic expansion, before building any pipeline.

Ceiling: of the chrono prose failures, how many have a query term whose
associate actually appears in the answer file? That is the most this mechanism
could ever fix.

Fan-out: how many index terms would an expansion light up? This is the number
that predicted the graph-expansion failure -- 182 projects boosted to find one
answer -- and it is what to watch.

Kill declared in advance: bridge for <= 4 of 28, or median fan-out > 5% of the
vocabulary.
"""
import json
import re
import sys

sys.path.insert(0, 'repograph')
import repograph as RG  # noqa: E402

S = ('C:/Users/Owen/AppData/Local/Temp/claude/'
        'C--Users-Owen-OneDrive-Documents-GitHub-awesome-rust/'
        '4bdbf542-95d3-407b-a23b-bde0211515bf/scratchpad')

assoc = json.load(open(S + '/assoc.json', encoding='utf-8'))['assoc']
spec = json.load(open(S + '/git-chrono.json', encoding='utf-8'))
idx = RG.Index(S + '/rg/chrono-git')

lib = {s['file'] for s in idx.symbols if s.get('role', 'lib') == 'lib'}
names = {s['name'].lower() for s in idx.symbols}

# words per file, as the ranker sees them
per_file = {}
for i, sym in enumerate(idx.symbols):
    per_file.setdefault(sym['file'], set()).update(idx.bags[i])

qs = []
for q in spec['questions']:
    ok = [f for f in q['ok'] if f in lib]
    if ok:
        qs.append((q['q'], ok, 'git', None))
_t1, _t3, rows = RG.run_questions(idx, qs)


def names_ident(q):
    for w in re.findall(r'[A-Za-z_][A-Za-z0-9_]{3,}', q):
        if w.lower() in names:
            return True
    return False


# Housekeeping is not a question. "Upgrade to windows-bindgen 0.61" and
# "docs: fix minor typo" have no answer any search could give, and leaving
# them in makes the prose stratum look harder than it is.
JUNK = re.compile(r'^(upgrade|switch to|update|docs?:|bump|migrate|port to|'
                    r'move to|drop |remove |rename |reformat|refactor)', re.I)
prose = [(q, ok) for (q, ok, _s, _h), (_q2, _f, pos, _s2)
            in zip(qs, rows)
            if not names_ident(q) and pos != 0 and not JUNK.match(q)]
answers = {q: ok for q, ok in prose}

bridged, unbridged, fanouts = [], [], []
for q, ok in prose:
    target = set()
    for f in ok:
        target |= per_file.get(f, set())
    qt = RG.tokens(q)
    unmatched = [t for t in qt if t not in idx.idf]
    # A bridge only counts when the associate is DISCRIMINATING in this index.
    # `serde` or `min` appears somewhere in almost any large file, so an
    # associate landing in the answer's bag proves nothing unless the word is
    # rare enough to have picked that file out.
    hit = None
    for t in unmatched or qt:
        for a in assoc.get(t, [])[:3]:
            if a in target and idx.idf.get(a, 0) >= 0.55:
                hit = (t, a, round(idx.idf.get(a, 0), 2))
                break
        if hit:
            break
    (bridged if hit else unbridged).append((q, hit))
    # fan-out: index terms the expansion would activate
    lit = set()
    for t in (unmatched or qt):
        for a in assoc.get(t, [])[:3]:
            if a in idx.idf:
                lit.add(a)
    fanouts.append(len(lit))

vocab = len(idx.idf)
fanouts.sort()
med = fanouts[len(fanouts) // 2] if fanouts else 0
print('SEMANTIC EXPANSION PROBE -- %d chrono prose failures' % len(prose))
print('=' * 68)
print('  bridged  %d/%d  (a query term has an associate in the answer file)'
        % (len(bridged), len(prose)))
print('  fan-out  median %d index terms lit, of %d in the vocabulary (%.1f%%)'
        % (med, vocab, 100.0 * med / max(vocab, 1)))
print()
print('  KILL if bridged <= 4, or fan-out > 5%')
print()
for q, hit in bridged[:8]:
    print('    BRIDGE  %-40s via %s -> %s (idf %.2f)'
            % (q[:40], hit[0], hit[1], hit[2]))
for q, _h in unbridged[:6]:
    print('    none    %s' % q[:60])
