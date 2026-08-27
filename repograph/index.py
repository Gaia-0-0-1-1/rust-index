"""Index a Rust repository: symbols, docs, references, doc coverage.

Point it at any checkout and it writes an index/ next to wherever you ask.
Nothing is fetched and nothing is built -- it reads .rs files and nothing else.

    python repograph/index.py /path/to/crate -o /path/to/index

The extractor is regex and brace counting rather than a real parser. That is a
deliberate trade and it was measured before being relied on: reference edges
came out 93% precise on a 30-edge hand-check, and symbol recall matched a manual
count (30 found against 28 by grep in one file, 110 against 107 in another --
it catches `macro_rules!` and `pub(crate)` forms a naive grep misses). Good
enough to rank on; see repograph/README.md for where it is not.
"""
import json
import os
import re
import sys

# `pub`, `pub(crate)`, `async`, `unsafe`, `const`, `extern "C"`, `default`
MODS = (r'(?:pub(?:\s*\([^)]*\))?\s+|async\s+|unsafe\s+|const\s+|default\s+'
        r'|extern\s+"[^"]*"\s+)*')
DECL = re.compile(
    r'^(?P<indent>[ \t]*)' + MODS +
    r'(?P<kind>fn|struct|enum|trait|type|union|macro_rules!|mod|static|const)'
    r'\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)')
USE = re.compile(r'^\s*(?:pub\s+)?use\s+([^;]+);')
IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]{2,}')
SKIP_DIRS = {'target', '.git', 'node_modules', '.github'}
# Directories that are part of the repository but not part of the library.
# Indexed and kept, because "where is the benchmark for X" is a fair question,
# but excluded from coverage and from ranking -- somebody asking how PNG is
# decoded does not want a benchmark harness.
ROLES = {'benches': 'bench', 'examples': 'example', 'tests': 'test',
    'fuzz': 'fuzz'}


def file_role(rel):
    head = rel.split('/')[0]
    return ROLES.get(head, 'lib')


def strip_noise(line):
    """Drop line comments and string bodies so brace counting is not fooled by
    a `{` inside a doc example or a format string."""
    out, i, in_str, esc = [], 0, False, False
    while i < len(line):
        c = line[i]
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == '/' and i + 1 < len(line) and line[i + 1] == '/':
            break
        else:
            out.append(c)
        i += 1
    return ''.join(out)


def module_doc(lines):
    """The `//!` block at the top of a file. It says what the file is for, in
    prose, which makes it both the best short description of a file and the
    material the eval generator turns into questions."""
    out = []
    for ln in lines[:200]:
        s = ln.strip()
        if s.startswith('//!'):
            out.append(s[3:].strip())
        elif s and not s.startswith('//') and not s.startswith('#!'):
            break
    return ' '.join(out).strip()


def parse_file(path, rel):
    text = open(path, encoding='utf-8', errors='replace').read()
    lines = text.split('\n')
    symbols, uses, tests = [], [], []

    for i, line in enumerate(lines):
        u = USE.match(line)
        if u:
            uses.append(u.group(1).strip())

        m = DECL.match(line)
        if not m:
            continue
        kind, name = m.group('kind'), m.group('name')

        # Doc comment and #[test], both found by walking back over the
        # attributes that sit between an item and its documentation.
        doc, is_test = [], False
        j = i - 1
        while j >= 0:
            s = lines[j].strip()
            if s.startswith('///'):
                # A line of slashes is a section divider, not documentation.
                # `//////////` begins with `///`, so it was being captured as
                # doc text -- harmless at 0.2% of symbols, but it is noise in
                # the one field retrieval depends on.
                body = s[3:].strip()
                if re.search(r'[A-Za-z0-9]', body):
                    doc.append(body)
            elif s.startswith('#['):
                if 'test' in s:
                    is_test = True
            elif not s:
                pass
            else:
                break
            j -= 1
        doc.reverse()

        depth, end, started = 0, i, False
        for k in range(i, min(len(lines), i + 6000)):
            clean = strip_noise(lines[k])
            depth += clean.count('{') - clean.count('}')
            if '{' in clean:
                started = True
            if started and depth <= 0:
                end = k
                break
            if not started and ';' in clean:
                end = k
                break

        sym = {
            'name': name,
            'kind': kind.rstrip('!'),
            'file': rel,
            'role': file_role(rel),
            'line': i + 1,
            'end': end + 1,
            'doc': ' '.join(doc).strip(),
            'sig': line.strip()[:200],
            'test': is_test,
        }
        symbols.append(sym)
        if is_test:
            tests.append({'name': name, 'file': rel, 'line': i + 1})

    return symbols, uses, module_doc(lines), tests


def walk(root):
    symbols, uses, mods, tests = [], {}, {}, []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in sorted(files):
            if not f.endswith('.rs'):
                continue
            p = os.path.join(base, f)
            rel = os.path.relpath(p, root).replace('\\', '/')
            try:
                s, u, md, t = parse_file(p, rel)
            except OSError:
                continue
            symbols.extend(s)
            uses[rel] = u
            tests.extend(t)
            if md:
                mods[rel] = md
    return symbols, uses, mods, tests


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


def is_public(sig):
    """`pub` alone is public. `pub(crate)`, `pub(super)` and `pub(in path)` are
    restricted, and counting them as API is most of why syn looked badly
    documented: `pub(crate) mod parsing` is not something a user can call."""
    sig = sig.lstrip()
    if not sig.startswith('pub'):
        return False
    rest = sig[3:]
    return not rest.startswith('(')


def mark_visibility(symbols):
    """A `pub fn` inside a private `mod` is not public API.

    syn declares most of its parsing in `mod parsing`, which is private, so 766
    of its 871 "undocumented public functions" were never public at all --
    which is most of why it reported 30% documented against chrono's 83%.
    Enclosing modules are found by span containment, which the extractor
    already records.
    """
    mods = [s for s in symbols if s['kind'] == 'mod']
    by_file = {}
    for m in mods:
        by_file.setdefault(m['file'], []).append(m)
    for s in symbols:
        pub = is_public(s['sig'])
        if pub:
            for m in by_file.get(s['file'], ()):
                if m is s or not (m['line'] < s['line'] <= m['end']):
                    continue
                if not is_public(m['sig']):
                    pub = False
                    break
        s['public'] = pub
    return symbols


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


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    root = os.path.abspath(argv[1])
    out = None
    if '-o' in argv:
        out = argv[argv.index('-o') + 1]
    if not out:
        out = os.path.join(root, '.repograph')
    if not os.path.isdir(root):
        print('not a directory: %s' % root)
        return 1

    symbols, uses, mods, tests = walk(root)
    symbols = mark_visibility(symbols)
    if not symbols:
        print('no .rs files found under %s' % root)
        return 1
    edges = build_edges(root, symbols)
    cov = coverage(symbols, mods, len(uses))

    os.makedirs(out, exist_ok=True)
    meta = {
        'root': root.replace('\\', '/'),
        'name': os.path.basename(root),
        'coverage': cov,
    }
    for fname, obj in (('symbols.json', symbols), ('edges.json', edges),
                        ('uses.json', uses), ('modules.json', mods),
                        ('tests.json', tests), ('meta.json', meta)):
        with open(os.path.join(out, fname), 'w', encoding='utf-8') as fh:
            json.dump(obj, fh)

    print('%s: %d symbols, %d files, %d edges, %d tests'
            % (meta['name'], len(symbols), len(uses), len(edges), len(tests)))
    print('  documented (%s): %d/%d (%.0f%%)'
            % (cov['basis'], cov['documented_of_basis'], cov['basis_size'],
                100 * cov['doc_ratio']))
    print('  written to %s' % out)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
