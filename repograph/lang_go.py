"""Go extractor, producing the same artifacts as the Rust one.

Go's example convention is the strongest of any language here and the thinnest
in practice, and both halves of that were measured before this was written.

    `func ExampleFoo()` in a _test.go file is compiled by `go test`, and where
    it carries an `// Output:` comment the runtime output is compared against
    it. That is stricter than Rust: the example is verified to produce what it
    claims, not merely to compile.

    The Go standard library has 39,729 exported functions and 1,015 Example
    functions -- 2.6%, of which 82% are output-checked. Third-party code
    measured lower still, 1.9% across cobra, mux and logrus. Rust carries a
    fenced example on 20% of documented symbols.

So `example` will find nothing far more often here than in Rust. The examples
it does find are better evidence than Rust's.

Examples bind to their subject by name, which is a convention the language
enforces rather than a guess: ExampleFoo documents Foo, ExampleFoo_bar is a
variant of it, and ExampleType_Method documents that method.
"""
import os
import re

NAME = 'go'
SUFFIX = '.go'

VOCAB = {
    'unit': 'package',
    'units': 'packages',
    'verifier': 'go test',
    'manifest': 'go.mod',
}

# Go's guarantee is the strongest available and it is not Rust's, so it gets
# its own words. An `// Output:` comment is compared against what the code
# really prints, which verifies the example does what it claims rather than
# merely that it compiles. An Example without one still runs, and saying so
# separately is the difference between honest and flattering.
VERIFICATION = {
    'runs': ('VERIFIED -- `go test` runs this and checks its `// Output:`'
        ' against what it really prints', True),
    'compiles': ('RUNS, UNCHECKED -- `go test` builds and runs this, but it'
        ' declares no expected output', False),
    'unverified': ('NOT VERIFIED -- nothing checks this', False),
    'failure': ('DEMONSTRATES FAILURE -- this code is meant to break', False),
}

# Go has no feature flags, so Rust's caveat about them does not apply.
COPY_CAVEAT = None

EVAL = {'module_doc': True, 'doc_split': True, 'test_name': True}

# Empty, and that is now a measurement rather than a deferral. The guess was
# that Go text is thick with err, ctx and nil and that dropping them would
# help. It is not: the index holds doc prose and signatures, not bodies, so
# those words never appear. The genuinely frequent tokens are `func` (in 80% of
# symbols, from every signature), `test`, `testing` and `method` -- and IDF has
# already crushed them to 0.04-0.19. Adding all of them changes nothing:
# logrus git goes 44/69 to 44/68 and the generated set does not move at all.
STOP_EXTRA = set()


def subject_file(rel):
    """Go keeps `entry_test.go` beside the `entry.go` it exercises, so a
    question derived from a test name is really a question about the latter.
    Pointing it at the test file asks for something the ranker never returns --
    test functions are excluded from results -- and that alone dragged the
    generated test-name stratum to 11% against Rust's 29%."""
    if rel.endswith('_test.go'):
        return rel[:-len('_test.go')] + '.go'
    return rel


def gate_of(sym):
    """Go constrains a file with a build tag rather than a cargo feature. The
    kind travels with the value because the advice differs: `linux` is a
    platform you are or are not on, not something to switch on in a manifest."""
    t = sym.get('build_tag')
    return ('build-tag', t) if t else None


DECL_FUNC = re.compile(
    r'^func\s+(?:\((?P<recv>[^)]*)\)\s*)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)')
DECL_TYPE = re.compile(
    r'^type\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+(?P<kind>struct|interface|'
    r'func|map|\[|chan|\*)?')
DECL_VAL = re.compile(
    r'^(?P<kind>const|var)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)')
PKG = re.compile(r'^package\s+([A-Za-z_][A-Za-z0-9_]*)')
IMPORT = re.compile(r'^\s*(?:[A-Za-z_.][A-Za-z0-9_]*\s+)?"([^"]+)"')
IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]{2,}')
EXAMPLE = re.compile(r'^Example(?P<subject>[A-Za-z0-9_]*)$')
# `const (` / `var (` / `type (` open a block declaring several names at once,
# and Go leans on them for enums and error codes. Reading only the opening line
# loses every name inside: ~107 across cobra, mux and logrus, about 7% of their
# symbols, invisible to search.
GROUP_OPEN = re.compile(r'^(?P<kind>const|var|type)\s*\($')
GROUP_NAME = re.compile(
    r'^\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*'
    r'(?:\s|=|$)')
# A build constraint gates a whole file. It is Go's analogue of a cargo
# feature, but not the same advice: `linux` is a platform you are or are not
# on, not a switch in a manifest, which is why gate_of carries the kind.
BUILD_TAG = re.compile(r'^//(?:go:build|\s*\+build)\s+(.+)$')
OUTPUT = re.compile(r'^\s*//\s*(Unordered )?[Oo]utput:', re.M)
SKIP_DIRS = {'.git', 'vendor', 'testdata', 'node_modules', '.github'}


def doc_above(lines, i):
    """Go documentation is a run of `//` lines immediately above the item, with
    no blank line between. A blank line ends it, which is how a file-header
    comment is kept out of the first declaration's docs."""
    out = []
    j = i - 1
    while j >= 0:
        s = lines[j].strip()
        if s.startswith('//'):
            out.append(s[2:].strip())
            j -= 1
            continue
        break
    out.reverse()
    return ' '.join(out).strip()


def build_tag(lines):
    """The build constraint above `package`, if any."""
    for ln in lines[:60]:
        s = ln.strip()
        if s.startswith('package '):
            return None
        m = BUILD_TAG.match(s)
        if m:
            return m.group(1).strip()
    return None


def package_doc(lines):
    """The comment block directly above `package x`. Go's equivalent of a
    module header, and the same thing `start` reads."""
    for i, ln in enumerate(lines):
        if PKG.match(ln.strip()):
            return doc_above(lines, i)
    return ''


def body_span(lines, i):
    """Brace counting, same trade as the Rust extractor. Go has no macros and
    no lifetimes, so this is markedly less lossy here."""
    depth, started = 0, False
    for k in range(i, min(len(lines), i + 4000)):
        line = re.sub(r'//.*$', '', lines[k])
        line = re.sub(r'"(?:[^"\\]|\\.)*"', '""', line)
        depth += line.count('{') - line.count('}')
        if '{' in line:
            started = True
        if started and depth <= 0:
            return k
        if not started and line.rstrip().endswith(')') and k > i:
            return k
    return i


def parse_file(path, rel):
    text = open(path, encoding='utf-8', errors='replace').read()
    lines = text.split('\n')
    symbols, imports, examples = [], [], []
    is_test = rel.endswith('_test.go')
    tag = build_tag(lines)
    in_group = None

    for i, line in enumerate(lines):
        # Inside `const (` ... `)` every indented line declares a name, and Go
        # leans on that for enums and error codes. Reading only the opening
        # line lost ~107 names across three repositories, about 7% of their
        # symbols, invisible to search.
        if in_group is not None:
            if line.strip() == ')':
                in_group = None
                continue
            gm = GROUP_NAME.match(line)
            if gm and not line.strip().startswith('//'):
                nm = gm.group('name')
                if nm != '_':
                    symbols.append({
                        'name': nm, 'kind': in_group, 'file': rel,
                        'line': i + 1, 'end': i + 1,
                        'doc': doc_above(lines, i),
                        'sig': line.strip()[:200], 'test': False,
                        'public': nm[:1].isupper(), 'examples': [],
                        'build_tag': tag,
                    })
            continue
        gopen = GROUP_OPEN.match(line.strip())
        if gopen:
            in_group = gopen.group('kind')
            continue

        m = IMPORT.match(line) if line.startswith('\t') or line.startswith(' ') \
            else None
        if m and '"' in line and not line.strip().startswith('//'):
            imports.append(m.group(1))

        kind = name = None
        recv = None
        fm = DECL_FUNC.match(line)
        if fm:
            kind, name, recv = 'func', fm.group('name'), fm.group('recv')
        else:
            tm = DECL_TYPE.match(line)
            if tm:
                kind = tm.group('kind') or 'type'
                kind = {'struct': 'struct', 'interface': 'interface'}.get(
                    kind, 'type')
                name = tm.group('name')
            else:
                vm = DECL_VAL.match(line)
                if vm:
                    kind, name = vm.group('kind'), vm.group('name')
        if not name:
            continue

        end = body_span(lines, i)
        doc = doc_above(lines, i)
        ex = EXAMPLE.match(name)
        if ex and is_test:
            code = '\n'.join(lines[i:end + 1])
            examples.append({
                'subject': ex.group('subject').split('_')[0],
                'name': name,
                'file': rel,
                'line': i + 1,
                'code': code,
                # Compiled by `go test` either way; `// Output:` means the
                # runtime result is compared too, which is why it is a
                # different bucket rather than a footnote.
                'kind': 'runs' if OUTPUT.search(code) else 'compiles',
                'doc': doc,
            })
            continue

        symbols.append({
            'name': name,
            'kind': 'method' if recv else kind,
            'file': rel,
            'line': i + 1,
            'end': end + 1,
            'doc': doc,
            'sig': line.strip()[:200],
            'test': is_test and name.startswith(('Test', 'Benchmark', 'Fuzz')),
            # Go's visibility is its capitalisation. No inference needed, and
            # none of the pub(crate)-versus-pub confusion the Rust side had.
            'public': name[:1].isupper(),
            'examples': [],
            'build_tag': tag,
        })
    return symbols, imports, package_doc(lines), examples


def walk(root):
    symbols, uses, mods, tests, allex = [], {}, {}, [], []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in sorted(files):
            if not f.endswith('.go'):
                continue
            p = os.path.join(base, f)
            rel = os.path.relpath(p, root).replace('\\', '/')
            try:
                syms, imps, pdoc, exs = parse_file(p, rel)
            except OSError:
                continue
            symbols.extend(syms)
            uses[rel] = imps
            allex.extend(exs)
            for s in syms:
                if s['test']:
                    tests.append({'name': s['name'], 'file': rel,
                        'line': s['line']})
            if pdoc:
                mods[rel] = pdoc

    # Bind examples to what they document. The language names them for it, so
    # this is a lookup rather than a guess -- ExampleParse documents Parse.
    by_name = {}
    for s in symbols:
        by_name.setdefault(s['name'], []).append(s)
    orphan = []
    for e in allex:
        # Same package only. An Example lives beside what it documents, and
        # without that constraint ExampleRegexp attaches to all 27 symbols
        # named Regexp -- harmless in a 500-symbol repository and badly wrong
        # in the standard library, where it produced 3,821 symbols carrying
        # examples against roughly 1,015 that exist.
        pkg = e['file'].rsplit('/', 1)[0] if '/' in e['file'] else ''
        targets = [t for t in (by_name.get(e['subject']) or [])
            if (t['file'].rsplit('/', 1)[0] if '/' in t['file'] else '') == pkg]
        if not targets:
            orphan.append(e)
            continue
        for t in targets:
            t['examples'].append({'code': e['code'], 'kind': e['kind'],
                'from': '%s:%d' % (e['file'], e['line'])})
    # A package-level `func Example()` documents the package, so it belongs
    # with the package header rather than nowhere.
    mod_ex = {}
    for e in orphan:
        pkgfile = e['file']
        mod_ex.setdefault(pkgfile.rsplit('/', 1)[0] + '/' if '/' in pkgfile
            else '', []).append({'code': e['code'], 'kind': e['kind']})
    return symbols, uses, mods, tests, mod_ex


def role_of(rel):
    """`_test.go` is where Go keeps its examples, so it cannot simply be
    excluded the way Rust's tests/ directory is. Test *functions* are filtered
    from ranking by their `test` flag instead."""
    head = rel.split('/')[0]
    if head in ('examples', 'example', '_examples'):
        return 'example'
    return 'lib'


TYPE_NAME = re.compile(r"\b((?:[a-z][A-Za-z0-9_]*\.)?[A-Z][A-Za-z0-9_]*)\b")


def groups(sig):
    """Every top-level parenthesised group, in order."""
    out, depth, start = [], 0, None
    for i, c in enumerate(sig):
        if c == '(':
            depth += 1
            if depth == 1:
                start = i + 1
        elif c == ')':
            depth -= 1
            if depth == 0 and start is not None:
                out.append((start, i))
                start = None
    return out


def signature_types(sig):
    """(parameter types, return types) from a Go signature.

    Go puts the result after the parameter list, bare or parenthesised, so the
    split is positional rather than on a token like Rust's `->`. A method
    writes its receiver first -- `func (r *Router) Handle(p string) error` --
    and reading that group as the parameters puts the real ones in the return
    slot along with the method's own name, which is what a first attempt did.

    Qualified names are kept whole: `http.Handler` is more useful than
    `Handler`, and it is what makes the reference resolvable against an index
    of the standard library.
    """
    sig = (sig or '').split('{')[0]
    gs = groups(sig)
    if not gs:
        return [], []
    head = sig[:gs[0][0] - 1].strip()
    # `func (` means the first group is a receiver, so parameters are the next.
    is_method = head.endswith('func') or head == 'func'
    if is_method and len(gs) >= 2:
        pstart, pend = gs[1]
    else:
        pstart, pend = gs[0]
    params = sig[pstart:pend]
    rest = sig[pend + 1:]
    # A multi-value result is parenthesised; a single one is bare.
    tail = groups(rest)
    if tail and rest[:tail[0][0] - 1].strip() == '':
        rest = rest[tail[0][0]:tail[0][1]]
    return TYPE_NAME.findall(params), TYPE_NAME.findall(rest)
