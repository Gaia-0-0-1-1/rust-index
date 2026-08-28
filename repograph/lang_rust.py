"""Rust extraction: the language plugin for `.rs` sources.

Cut verbatim out of index.py when Go arrived and the shared driver needed to
stop assuming Rust. Nothing here was rewritten in the move -- the acceptance
test was that every artifact came out byte-identical -- because each rewrite
attempted in this codebase introduced a bug: `unhide` deleted `#[derive]` from
examples it called verified, a widened `module_doc` cost three points of
retrieval, and the fence scanner desynchronised on a crate that opens a fence
from an attribute.

The contract a language plugin fills is described in index.py.
"""
import os
import re

NAME = 'rust'
SUFFIX = '.rs'

# What this language calls things, so the shared query surface can speak it
# rather than saying "crate" at a Go user.
VOCAB = {
    'unit': 'crate',
    'units': 'crates',
    'verifier': 'cargo test --doc',
    'manifest': 'Cargo.toml',
}

# What each example bucket means HERE, and whether it may be copied. Declaring
# this as data is what lets a language with no verification at all -- C, whose
# Doxygen blocks are never compiled -- say so rather than fall through a
# missing branch.
VERIFICATION = {
    'runs': ("VERIFIED -- the crate's own doc tests compile and run this",
        True),
    'compiles': ('COMPILES ONLY -- marked no_run, never executed', False),
    'unverified': ('NOT VERIFIED -- marked ignore, or not Rust', False),
    'failure': ('DEMONSTRATES FAILURE -- this code is meant to break', False),
}

# Doc tests run inside the crate with every feature and dev-dependency
# available; a reader copying the code has neither. Go has no equivalent, so
# this caveat belongs to the language rather than to the printer.
COPY_CAVEAT = ('doc tests run with all features and dev-dependencies present;'
    ' yours may not be')

# Which eval generators this language can supply primitives for. A generator
# whose primitive is missing is skipped loudly, never silently.
EVAL = {'module_doc': True, 'doc_split': True, 'test_name': True}

# Retrieval-relevant, so it lives in the contract -- but Go's values are
# deliberately identical to these for now. Go-specific stop words change Go's
# numbers and must ship as a measured change, not inside a refactor.
STOP_EXTRA = set()

SKIP_DIRS = {'target', '.git', 'node_modules', '.github'}

# `pub`, `pub(crate)`, `async`, `unsafe`, `const`, `extern "C"`, `default`
MODS = (r'(?:pub(?:\s*\([^)]*\))?\s+|async\s+|unsafe\s+|const\s+|default\s+'
        r'|extern\s+"[^"]*"\s+)*')
DECL = re.compile(
    r'^(?P<indent>[ \t]*)' + MODS +
    r'(?P<kind>fn|struct|enum|trait|type|union|macro_rules!|mod|static|const)'
    r'\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)')
USE = re.compile(r'^\s*(?:pub\s+)?use\s+([^;]+);')
# `#[cfg(feature = "raw_value")]` directly above `mod raw;` gates every symbol
# in raw.rs behind that feature. Without this the tool calls a doc example
# VERIFIED and a reader copies code that will not build, because the crate's
# own doc tests run with all features on and theirs do not.
CFG_MOD = re.compile(
    r'#\[cfg\(feature\s*=\s*"([^"]+)"\)\]\s*'
    r'(?:pub\s+)?mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*;')
IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]{2,}')
# Documentation can also arrive as an attribute, and `toml` opens a fence that
# way: `#![cfg_attr(feature = "default", doc = " ```")]`. A scanner that reads
# only `//!` never sees that fence, desynchronises, and swallows the prose
# after it into the next example -- which is how markdown headings ended up
# being presented as verified code.
DOC_ATTR = re.compile(
    r'#!?\[(?:cfg_attr\([^,]*,\s*)?doc\s*=\s*"((?:[^"\\]|\\.)*)"')
# Directories that are part of the repository but not part of the library.
# Indexed and kept, because "where is the benchmark for X" is a fair question,
# but excluded from coverage and from ranking -- somebody asking how PNG is
# decoded does not want a benchmark harness.
ROLES = {'benches': 'bench', 'examples': 'example', 'tests': 'test',
    'fuzz': 'fuzz'}


def role_of(rel):
    head = rel.split('/')[0]
    return ROLES.get(head, 'lib')
# What a doc example demonstrates, which is not the same as whether cargo runs
# it. `should_panic` compiles, runs and passes -- while demonstrating a crash --
# so a flag meaning "cargo verified this" would hand a reader a panic as working
# code. Only RUNS may be presented as something to copy.
RUNS, COMPILES, UNVERIFIED, FAILURE = 'runs', 'compiles', 'unverified', 'failure'
NOT_RUST = {'text', 'notrust', 'json', 'toml', 'yaml', 'python', 'sh', 'bash',
    'console', 'markdown', 'md', 'html', 'c', 'cpp'}


def classify_fence(info):
    """Rust doc fences carry comma-separated flags: ```rust,no_run and
    ```ignore,json both occur. Classify by the flags present, never by the
    whole string."""
    flags = {f.strip().lower() for f in (info or '').split(',') if f.strip()}
    if flags & {'compile_fail', 'should_panic'}:
        return FAILURE
    if flags & {'ignore'} or flags & NOT_RUST:
        return UNVERIFIED
    if 'no_run' in flags:
        return COMPILES
    return RUNS


def unhide(line):
    """Resolve rustdoc's hidden-line marker inside a doc example.

    A line is hidden only when it is `#` followed by a space, or `#` alone.
    Strip the marker and keep the content -- those lines are usually the
    imports the example needs to compile, and dropping them breaks it.

    `#[derive(Debug)]` is NOT hidden, it is an attribute, and an earlier
    version of this deleted every one of them because they also begin with
    `#`. That silently produced examples that could never compile, which is
    the worst possible failure for a feature whose whole claim is that the
    code is known good.

    `##` is an escaped literal `#`, as in a shebang or a nested attribute.
    """
    body = line.lstrip()
    if not body.startswith('#'):
        return line
    if body.startswith('##'):
        return line.replace('##', '#', 1)
    rest = body[1:]
    if not rest.strip():
        return None                      # `#` alone: a hidden blank line
    if rest.startswith(' '):
        return rest[1:]                  # `# use foo::Bar;` -> keep content
    return line                          # `#[derive(..)]`, `#![feature]`


def doc_examples(raw_lines):
    """Fenced blocks from a run of `///` lines, with structure preserved.

    The prose `doc` field flattens everything to one line, which is right for
    matching words and useless for code. Examples are kept apart, with their
    newlines, so they can be handed over as something runnable.
    """
    out, code, info, inside = [], [], None, False
    for ln in raw_lines:
        stripped = ln.strip()
        if stripped.startswith('```'):
            if inside:
                out.append({'code': '\n'.join(code).strip(),
                    'kind': classify_fence(info)})
                code, inside = [], False
            else:
                inside, info, code = True, stripped[3:].strip(), []
            continue
        if inside:
            kept = unhide(ln)
            if kept is not None:
                code.append(kept)
    # An unterminated block means the fences did not balance and the scanner
    # lost its place. Emitting what it collected would hand over prose labelled
    # as verified code, so throw it away.
    return [e for e in out if e['code']]


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
    """The `//!` block at the top of a file, as prose and as examples.

    A crate's `lib.rs` header is its author's own getting-started guide, and
    the fenced blocks in it are the code they chose to lead with -- compiled
    and run by the same doc tests as everything else. That makes it the best
    answer to "how do I use this" that exists anywhere in the repository, and
    picking it requires no ranking at all: the author already chose.

    The prose half is deliberately byte-for-byte what it was before examples
    were added. It feeds the retrieval bags, and widening the scan or filtering
    blank fence lines out of it moved the blind score from 28/54 to 25/53 --
    a presentation feature is not allowed to cost retrieval accuracy.
    """
    out, raw = [], []
    attr_fence = False
    for ln in lines[:200]:
        s = ln.strip()
        if s.startswith('//!'):
            attr_fence = False
            out.append(s[3:].strip())
            body = ln.strip()[3:]
            raw.append(body[1:] if body.startswith(' ') else body)
        elif s.startswith('#!['):
            # Only the raw stream gets these. The prose half feeds retrieval
            # and is deliberately unchanged; this is purely so the fence
            # counting stays in step.
            m = DOC_ATTR.search(s)
            if m:
                body = m.group(1).replace('\\"', '"')
                body = body[1:] if body.startswith(' ') else body
                # Feature-gated fences come in mutually exclusive pairs:
                #     #![cfg_attr(not(feature = "x"), doc = " ```ignore")]
                #     #![cfg_attr(feature = "x",      doc = " ```")]
                # Exactly one is live in any build, but a reader that takes
                # both sees a phantom open-and-close and every later fence
                # lands on the wrong side. Collapse a consecutive pair to one.
                if body.strip().startswith('```') and attr_fence:
                    # Keep whichever half describes a default build. `toml`
                    # writes the not(feature) branch first, so taking the
                    # earlier one labels genuinely runnable code `ignore` --
                    # understating it, which is the opposite error and equally
                    # wrong.
                    if 'ignore' in raw[-1] and 'ignore' not in body:
                        raw[-1] = body
                    continue
                attr_fence = body.strip().startswith('```')
                raw.append(body)
                continue
            attr_fence = False
        elif s and not s.startswith('//') and not s.startswith('#!'):
            # A non-doc inner attribute is skipped, not treated as the end of
            # the header. That is the behaviour this had before doc attributes
            # were parsed, and nothing measured justifies changing it.
            break
    return ' '.join(out).strip(), doc_examples(raw)


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
        doc, raw, is_test = [], [], False
        j = i - 1
        while j >= 0:
            s = lines[j].strip()
            if s.startswith('///'):
                # A line of slashes is a section divider, not documentation.
                # `//////////` begins with `///`, so it was being captured as
                # doc text -- harmless at 0.2% of symbols, but it is noise in
                # the one field retrieval depends on.
                body = s[3:]
                raw.append(body[1:] if body.startswith(' ') else body)
                if re.search(r'[A-Za-z0-9]', body.strip()):
                    doc.append(body.strip())
            elif s.startswith('#['):
                if 'test' in s:
                    is_test = True
            elif not s:
                pass
            else:
                break
            j -= 1
        doc.reverse()
        raw.reverse()

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
            'role': role_of(rel),
            'line': i + 1,
            'end': end + 1,
            'doc': ' '.join(doc).strip(),
            'sig': line.strip()[:200],
            'test': is_test,
            'examples': doc_examples(raw),
        }
        symbols.append(sym)
        if is_test:
            tests.append({'name': name, 'file': rel, 'line': i + 1})

    prose, mod_ex = module_doc(lines)
    return symbols, uses, prose, tests, mod_ex

def feature_gates(root):
    """file -> feature that must be enabled for it to exist."""
    gates = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if not f.endswith('.rs'):
                continue
            p = os.path.join(base, f)
            try:
                text = open(p, encoding='utf-8', errors='replace').read()
            except OSError:
                continue
            here = os.path.relpath(base, root).replace('\\', '/')
            here = '' if here == '.' else here + '/'
            for feat, name in CFG_MOD.findall(text):
                for cand in ('%s%s.rs' % (here, name),
                                '%s%s/mod.rs' % (here, name)):
                    gates[cand] = feat
    return gates

def walk(root):
    symbols, uses, mods, tests, mod_ex = [], {}, {}, [], {}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in sorted(files):
            if not f.endswith('.rs'):
                continue
            p = os.path.join(base, f)
            rel = os.path.relpath(p, root).replace('\\', '/')
            try:
                s, u, md, t, mx = parse_file(p, rel)
            except OSError:
                continue
            symbols.extend(s)
            uses[rel] = u
            tests.extend(t)
            if md:
                mods[rel] = md
            if mx:
                mod_ex[rel] = mx
    return symbols, uses, mods, tests, mod_ex

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


def subject_file(rel):
    """Rust keeps its unit tests in a `mod tests` inside the file they test,
    so a test is already about the file it lives in."""
    return rel


def gate_of(sym):
    """Rust gates a module behind a cargo feature."""
    f = sym.get('requires_feature')
    return ('feature', f) if f else None


# Type names inside a fragment of a signature. `Result<Decoder, Error>` yields
# Result, Decoder and Error -- all three, because a question about Decoder
# should find a function returning `Result<Decoder>`, which is exactly what
# grepping for `-> Decoder` misses.
TYPE_NAME = re.compile(r"\b([A-Z][A-Za-z0-9_]*)\b")
LIFETIME = re.compile(r"'[a-z_][A-Za-z0-9_]*")


def signature_types(sig):
    """(parameter types, return types) from a Rust signature.

    Deliberately shallow. `Result<Decoder>` gives both names rather than a
    parsed tree, because the question being answered is "what mentions Decoder
    in a return position", and a tree would buy nothing for it.
    """
    sig = LIFETIME.sub('', sig or '')
    if '(' not in sig:
        return [], []
    ret = []
    body = sig
    if '->' in sig:
        body, tail = sig.split('->', 1)
        ret = TYPE_NAME.findall(tail.split('{')[0].split('where')[0])
    depth, start, params = 0, None, ''
    for i, c in enumerate(body):
        if c == '(':
            if depth == 0:
                start = i + 1
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0 and start is not None:
                params = body[start:i]
                break
    # Generic parameters declared by the function are not types it consumes.
    declared = set()
    if '<' in body.split('(')[0]:
        declared = set(TYPE_NAME.findall(body.split('(')[0].split('<', 1)[1]))
    par = [t for t in TYPE_NAME.findall(params) if t not in declared]
    return par, [t for t in ret if t not in declared]
