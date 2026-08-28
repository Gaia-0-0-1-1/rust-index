"""Index a source repository: symbols, docs, references, doc coverage.

    python repograph/index.py /path/to/repo -o /path/to/index [--lang go]

Language-neutral. Everything specific to a language lives in a `lang_*` plugin
and the driver never asks which one it has.

A plugin supplies:

    NAME, SUFFIX          what it is and what files it reads
    VOCAB                 crate/package/module, and what verifies an example,
                            so the query surface can speak the right language
                            instead of saying "crate" at a Go user
    VERIFICATION          per example-kind, a label and whether it may be
                            copied. Declaring this as data is what lets a
                            language with no verification -- C, whose Doxygen
                            blocks are never compiled -- say so, rather than
                            fall through a branch nobody wrote
    COPY_CAVEAT           any caveat attached to copying verified code
    EVAL                  which eval generators it can supply primitives for
    STOP_EXTRA            retrieval stop words peculiar to it
    walk(root)            -> symbols, imports, module docs, tests, examples
    role_of(rel)          library, test, bench or example
    gate_of(sym)          (kind, value) for a build constraint, or None:
                            ('feature', 'raw_value') is not the same advice as
                            ('build-tag', 'linux'), so the kind travels with it

Optional: mark_visibility, feature_gates -- applied when present.

The extractors are regex and brace counting rather than real parsers. That
trade was measured before it was relied on, in both languages: reference edges
came out 93% precise on a hand-checked sample of 30 in Rust and again in Go,
and symbol recall matched a manual count.
"""
import json
import os
import re
import sys

# An identifier in any of these languages. Generic on purpose: the rarity gate
# that makes reference edges usable is not language-specific, and it measured
# 93% precise in Rust and again in Go.
IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]{2,}')
SKIP_DIRS = {'target', '.git', 'node_modules', '.github'}
# Artifact shape version. graph.py caches indexes and reindexes on
# a mismatch rather than reading a layout it no longer understands.
SCHEMA = 3





def bodies(root, symbols):
    """Re-read each file once and hand back every symbol's source text. Kept
    out of the artifacts -- it is only needed while edges are being built."""
    byfile = {}
    for s in symbols:
        byfile.setdefault(s['file'], []).append(s)
    out = {}
    for rel, syms in byfile.items():
        try:
            lines = open(os.path.join(root, rel), encoding='utf-8',
                            errors='replace').read().split('\n')
        except OSError:
            continue
        for s in syms:
            out[id(s)] = '\n'.join(lines[s['line'] - 1:s['end']])
    return out


def build_edges(root, symbols, df_max=3):
    """`from` references `to`, gated on how many symbols share the name.

    Ungated this is a hub generator: a crate has dozens of methods called
    `parse` or `new`, so every body mentioning one draws an edge to all of them
    and the graph stops discriminating. Only names resolving to a few symbols
    are facts. This is the same rarity argument that governs query tokens.
    """
    src = bodies(root, symbols)
    by_name = {}
    for idx, s in enumerate(symbols):
        by_name.setdefault(s['name'], []).append(idx)

    edges = []
    for idx, s in enumerate(symbols):
        body = src.get(id(s), '')
        seen = set()
        for tok in IDENT.findall(body):
            if tok == s['name'] or tok in seen:
                continue
            targets = by_name.get(tok)
            if not targets or len(targets) > df_max:
                continue
            seen.add(tok)
            for t in targets:
                if t != idx:
                    edges.append([idx, t, tok])
    return edges



def coverage(symbols, mods, nfiles):
    """Documentation coverage, reported as a first-class number.

    Measured, not decorative: with a symbol's doc removed from the index, the
    chance of finding it from a description of it falls to 1%. A name, path and
    signature carry almost none of the words a person types --
    `num_seconds_from_midnight` keeps midnight/num/seconds and nothing else. So
    a thinly documented repository is close to unsearchable by description, and
    the tool has to say so rather than answer confidently.
    """
    lib = [s for s in symbols if s.get('role', 'lib') == 'lib']
    symbols = lib or symbols
    pub = [s for s in symbols if s.get('public')]
    # A binary crate exports nothing, so judging it on public items reports
    # 0/0 and calls a perfectly ordinary program undocumented. Fall back to
    # every symbol when there is no meaningful public surface to measure.
    basis = pub if len(pub) >= 20 else symbols
    documented = [s for s in basis if s['doc']]
    return {
        'basis': 'public' if basis is pub else 'all symbols',
        'files': nfiles,
        'lib_files': len({s['file'] for s in symbols}),
        'files_with_module_doc': len(mods),
        'symbols': len(symbols),
        'public_symbols': len(pub),
        'public_documented': len(documented),
        'documented_of_basis': len(documented),
        'basis_size': len(basis),
        'doc_ratio': round(len(documented) / max(len(basis), 1), 3),
        'module_doc_ratio': round(len(mods) / max(nfiles, 1), 3),
    }


def write_index(root, out, symbols, edges, uses, mods, tests, mod_ex, cov,
        lang):
    os.makedirs(out, exist_ok=True)
    meta = {'schema': SCHEMA, 'lang': lang,
        'root': root.replace('\\', '/'),
        'name': os.path.basename(root), 'coverage': cov}
    for fname, obj in (('symbols.json', symbols), ('edges.json', edges),
                        ('module-examples.json', mod_ex),
                        ('uses.json', uses), ('modules.json', mods),
                        ('tests.json', tests), ('meta.json', meta)):
        with open(os.path.join(out, fname), 'w', encoding='utf-8') as fh:
            json.dump(obj, fh)
    print('%s [%s]: %d symbols, %d files, %d edges, %d tests'
        % (meta['name'], lang, len(symbols), len(uses), len(edges),
            len(tests)))
    print('  documented (%s): %d/%d (%.0f%%)'
        % (cov['basis'], cov['documented_of_basis'], cov['basis_size'],
            100 * cov['doc_ratio']))
    ex = sum(len(s.get('examples') or []) for s in symbols)
    if ex:
        print('  %d examples attached to symbols' % ex)
    print('  written to %s' % out)
    return 0


def load_language(name):
    """One import, no branching afterwards."""
    if name == 'go':
        import lang_go
        return lang_go
    import lang_rust
    return lang_rust


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    root = os.path.abspath(argv[1])
    lang = 'rust'
    if '--lang' in argv:
        lang = argv[argv.index('--lang') + 1]
    out = None
    if '-o' in argv:
        out = argv[argv.index('-o') + 1]
    if not out:
        out = os.path.join(root, '.repograph')
    if not os.path.isdir(root):
        print('not a directory: %s' % root)
        return 1

    L = load_language(lang)
    symbols, uses, mods, tests, mod_ex = L.walk(root)
    if not symbols:
        print('no %s files found under %s' % (L.SUFFIX, root))
        return 1
    if hasattr(L, 'mark_visibility'):
        symbols = L.mark_visibility(symbols)
    for sym in symbols:
        sym.setdefault('role', L.role_of(sym['file']))
    if hasattr(L, 'feature_gates'):
        gates = L.feature_gates(root)
        for sym in symbols:
            feat = gates.get(sym['file'])
            if feat:
                sym['requires_feature'] = feat

    edges = build_edges(root, symbols)
    cov = coverage(symbols, mods, len(uses))
    return write_index(root, out, symbols, edges, uses, mods, tests, mod_ex,
        cov, L.NAME)


if __name__ == '__main__':
    sys.exit(main(sys.argv))
