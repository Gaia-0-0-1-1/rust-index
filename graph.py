"""One front door over two engines.

    python graph.py use "parse a toml file"

`tools/rustgraph.py` knows which crate to reach for; `repograph/` knows how a
crate's code is laid out and carries its verified examples. Neither can answer
"I need to do X, show me working code" on its own. This chains them and does
not merge them -- they share no code deliberately, and merging would risk 75
selftest checks' worth of working software to gain nothing.

Both engines keep their own entry points. This adds a door, not a rewrite.

What the chain inherits, and does not launder:

    rustgraph picks the right crate first 59% of the time and within three 80%,
    so `use` shows three candidates rather than pretending to know.

    `start` finds an author-written introduction for roughly 60% of crates. A
    candidate without one says so in a line rather than padding with unverified
    fragments.

    Only 1,113 crates of the ~150k on crates.io are in the local registry, so a
    miss is the ordinary case away from the popular head, not an error.
"""
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
sys.path.insert(0, os.path.join(ROOT, 'repograph'))

CACHE = os.path.join(ROOT, '.cache', 'repograph')
VERSIONED = re.compile(r'^(.+?)-(\d+(?:\.\d+)*(?:[.+-][\w.+-]*)?)$')


def registry_root():
    """Where cargo unpacks crate sources. Absent on a machine that has never
    built anything, which is a normal state and not a failure."""
    base = os.path.expanduser('~/.cargo/registry/src')
    if not os.path.isdir(base):
        return None
    for entry in sorted(os.listdir(base)):
        full = os.path.join(base, entry)
        if os.path.isdir(full):
            return full
    return None


def version_key(v):
    """Sort versions so 1.13.0 beats 1.5.5, and a release beats its own
    pre-release. Wrong-version answers are a failure mode the chain introduces
    and neither engine alone can guard against."""
    core = re.split(r'[-+]', v, 1)
    nums = []
    for part in core[0].split('.'):
        nums.append(int(part) if part.isdigit() else 0)
    while len(nums) < 3:
        nums.append(0)
    prerelease = len(core) > 1 and core[1][:1].isalpha()
    return (nums, 0 if prerelease else 1, v)


def find_crate(name):
    """Highest non-prerelease version of `name` in the local registry."""
    reg = registry_root()
    if not reg:
        return None
    found = []
    for entry in os.listdir(reg):
        m = VERSIONED.match(entry)
        if m and m.group(1).lower() == name.lower().replace('_', '-'):
            found.append((version_key(m.group(2)), entry))
        elif m and m.group(1).lower().replace('-', '_') == name.lower():
            found.append((version_key(m.group(2)), entry))
    if not found:
        return None
    found.sort()
    return os.path.join(reg, found[-1][1])


def index_for(crate_path, quiet=True):
    """Index a crate, cached by its versioned directory name.

    Registry checkouts are immutable per version, so the only way a cache goes
    stale is the artifact format changing -- and symbols.json changed shape
    three times in a single day. The schema field decides.
    """
    import index as RGIDX
    name = os.path.basename(crate_path.rstrip('/\\'))
    out = os.path.join(CACHE, name)
    meta = os.path.join(out, 'meta.json')
    if os.path.exists(meta):
        try:
            if json.load(open(meta, encoding='utf-8')).get('schema') == \
                    RGIDX.SCHEMA:
                return out
        except (ValueError, OSError):
            pass
        shutil.rmtree(out, ignore_errors=True)
    os.makedirs(CACHE, exist_ok=True)
    cmd = [sys.executable, os.path.join(ROOT, 'repograph', 'index.py'),
            crate_path, '-o', out]
    subprocess.run(cmd, capture_output=quiet, text=True)
    return out if os.path.exists(meta) else None


def best_example(idx):
    """The most useful verified code this crate offers, or nothing.

    Author's own header first -- they chose what to lead with -- then the
    examples on the most widely referenced public item. Only `runs` is offered;
    an unverified fragment is worse than an honest blank.
    """
    import repograph as RG
    for path in ('src/lib.rs', 'lib.rs'):
        for e in idx.module_examples.get(path, []):
            if e.get('kind') == 'runs':
                return path, e, None
    ranked = sorted(
        ((idx.centrality.get(i, 0), i) for i, s in enumerate(idx.symbols)
            if s.get('public') and s.get('role', 'lib') == 'lib'
            and any(x.get('kind') == 'runs' for x in (s.get('examples') or []))),
        key=lambda x: -x[0])
    if ranked:
        sym = idx.symbols[ranked[0][1]]
        for e in sym['examples']:
            if e.get('kind') == 'runs':
                return '%s:%d' % (sym['file'], sym['line']), e, sym
    return None, None, None


def cmd_use(need, limit=3):
    import rustgraph as CRATES
    import repograph as RG

    nodes, cats = CRATES.load()
    hits, _tc, _sub, _conf = CRATES.rank(nodes, cats, need)
    if not hits:
        print('NO MATCH for %r in the curated list.' % need)
        return 1

    print('NEED  %s' % need)
    print('=' * 70)
    print('%d candidates. The list picks the right crate first about 59%% of'
        % limit)
    print('the time and within three about 80%, so read all of them.')

    shown = 0
    for _score, node in hits:
        if shown >= limit:
            break
        crate, verified = CRATES.crate_candidate(node)
        if not crate:
            continue
        shown += 1
        line = (node.get('lines') or [None])[0]
        print()
        print('-' * 70)
        print('%d. %s%s' % (shown, crate,
            '' if verified else '   [UNVERIFIED NAME -- check crates.io]'))
        if node.get('description'):
            print('   %s' % node['description'][:150])
        if line:
            print('   README.md:%d' % line)

        path = find_crate(crate)
        if not path:
            print('   not in the local registry -- `cargo fetch` would add it,')
            print('   and then this command could show its verified examples.')
            continue
        idxdir = index_for(path)
        if not idxdir:
            print('   could not index %s' % os.path.basename(path))
            continue
        idx = RG.Index(idxdir)
        print('   indexed %s' % os.path.basename(path))
        where, example, sym = best_example(idx)
        if not example:
            ex_total = sum(len(s.get('examples') or []) for s in idx.symbols)
            print('   no verified example: %d doc examples in the crate, none'
                % ex_total)
            print('   of them runnable and self-contained. Try'
                ' `graph.py crate %s start`.' % crate)
            continue
        print()
        print('   working code (%s):' % where)
        RG.show_example(example, indent='     ')
    return 0


def cmd_crate(argv):
    """Pass an indexed crate through to repograph."""
    if len(argv) < 2:
        print('usage: graph.py crate <name> <command> [args]')
        return 1
    name, rest = argv[0], argv[1:]
    path = find_crate(name)
    if not path:
        print('%s is not in the local registry. `cargo fetch` would add it.'
            % name)
        return 1
    idxdir = index_for(path)
    if not idxdir:
        print('could not index %s' % path)
        return 1
    import repograph as RG
    return RG.main([sys.argv[0], idxdir] + rest)


def cmd_selftest():
    """Gate the join itself. Neither engine can check any of this alone."""
    fails = []

    if version_key('1.13.0') <= version_key('1.5.5'):
        fails.append('version: 1.13.0 must sort above 1.5.5')
    if not version_key('1.0.0') > version_key('1.0.0-rc1'):
        fails.append('version: a release must beat its own pre-release')

    reg = registry_root()
    if reg:
        p = find_crate('serde_json')
        if p and not os.path.isdir(p):
            fails.append('resolve: returned a path that does not exist')
        if find_crate('this-crate-does-not-exist-xyzzy'):
            fails.append('resolve: invented a crate that is not there')

    import rustgraph as CRATES
    nodes, cats = CRATES.load()
    hits, _t, _s, _c = CRATES.rank(nodes, cats, 'parse a toml file')
    if not hits:
        fails.append('bridge: the crate engine answered nothing')

    for f in fails:
        print('  FAIL %s' % f)
    print('selftest %s (%d checks)' % ('OK' if not fails else 'FAILED', 5))
    return 1 if fails else 0


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, ValueError):
            pass
    if len(argv) < 2 or argv[1] in ('-h', '--help', 'help'):
        print(__doc__)
        print('Commands')
        print('    use "<need>"            which crate, and verified code for'
            ' it')
        print('    which "<need>"          which crate only (the curated list)')
        print('    crate <name> <cmd>...   ask about one crate\'s code')
        print('    selftest                check the join still holds')
        print()
        print('Both engines keep their own entry points:')
        print('    python tools/rustgraph.py ask "<need>"')
        print('    python repograph/repograph.py <index> <command>')
        return 0

    cmd, rest = argv[1], argv[2:]
    if cmd == 'use':
        return cmd_use(' '.join(rest))
    if cmd == 'which':
        import rustgraph as CRATES
        return CRATES.main([argv[0], 'ask'] + rest)
    if cmd == 'crate':
        return cmd_crate(rest)
    if cmd == 'selftest':
        return cmd_selftest()
    print('unknown command %r' % cmd)
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
