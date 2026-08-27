#!/usr/bin/env python3
"""rustgraph.py -- the agent-facing query surface over index/.

Built on the artifacts `cargo run --bin index` produces. Stdlib only, no network.

  ask <query...>     rank projects against a need, expand to the sibling
                     alternatives, and emit a context pack: the brief an agent
                     gets before choosing a crate
  cat [path]         list a category and its members (members of one category
                     are mutual alternatives)
  dag [--write]     dependency DAG: strata + load-bearing rank (needs crates_deps)
  node <name|crate>  everything known about one project
  stats              index shape

  python tools/rustgraph.py ask "async http client with rustls"
  python tools/rustgraph.py cat "Libraries/Command-line/Argument parsing"
  python tools/rustgraph.py node reqwest

Ranking note: in a hand-curated list the taxonomy carries more signal than the
prose, so categories are scored first and nodes inherit their category's score.
Matching on names alone makes "async" pull in async-std/async-dropper/
async-graphql by pure string overlap, which is noise.
"""

import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NODES = ROOT / "index" / "nodes.jsonl"
CATEGORIES = ROOT / "index" / "categories.json"
IDENTITY = ROOT / "index" / "crate-identity.json"
DOWNLOADS = ROOT / "index" / "downloads.json"
DEP_EDGES = ROOT / "index" / "dep-edges.jsonl"
DAG = ROOT / "index" / "dag.json"

# Filled by load(). Absent until tools/crates_deps.py has run.
IDENT = {}
DL = {}
CONFIDENT = {"readme", "repo-exact", "repo-alias"}

STOP = {
    "a", "an", "and", "for", "the", "with", "to", "in", "of", "on", "is", "it",
    "i", "want", "need", "using", "use", "how", "do", "build", "make", "my",
    "that", "this", "be", "can", "some", "library", "crate", "rust", "lib",
}


def tokens(s):
    return [t for t in re.split(r"[^a-z0-9+#.]+", s.lower()) if t and t not in STOP]


def load():
    global IDENT, DL
    if not NODES.exists():
        sys.exit("index/nodes.jsonl missing -- run: cargo run --bin index")
    nodes = [json.loads(l) for l in NODES.read_text(encoding="utf-8").splitlines() if l.strip()]
    cats = json.loads(CATEGORIES.read_text(encoding="utf-8")) if CATEGORIES.exists() else []
    if IDENTITY.exists():
        IDENT = json.loads(IDENTITY.read_text(encoding="utf-8"))
    if DOWNLOADS.exists():
        DL = json.loads(DOWNLOADS.read_text(encoding="utf-8"))
    return nodes, cats


def load_deps(kinds=("0",)):
    """from -> [deps]. 'from depends on to', matching the edge direction.

    Normal dependencies only by default. Dev-dependencies (kind 2) are not
    runtime edges and they close cycles -- serde dev-depends on serde_json,
    which depends on serde -- which fuses the graph into one component and
    makes every transitive-dependent count saturate at the same number."""
    adj = {}
    if not DEP_EDGES.exists():
        return adj
    for line in DEP_EDGES.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        if kinds and e.get("dep_kind") not in kinds:
            continue
        adj.setdefault(e["from"], set()).add(e["to"])
    return {k: sorted(v) for k, v in adj.items()}


def popularity(n):
    """Downloads if the dump has run, else GitHub stars, else nothing."""
    d = DL.get(n["id"])
    if d:
        return d, "dl"
    stars = (n.get("metrics") or {}).get("github_stars")
    return (stars, "star") if stars else (None, None)


def phrases(qt):
    """Longest contiguous token runs first: 'http client' before 'http'."""
    for n in range(len(qt), 0, -1):
        for i in range(len(qt) - n + 1):
            yield n, " ".join(qt[i:i + n])


def score_category(cat, qt):
    """A query naming a category ('http client') should surface that category,
    not every project whose description happens to contain 'http'."""
    path = " / ".join(cat["path"]).lower()
    leaf = cat["path"][-1].lower()
    s = 0.0
    for n, ph in phrases(qt):
        if n == 1:
            continue
        if ph == leaf:
            s += 14 * n
        elif ph in leaf:
            s += 7 * n
        elif ph in path:
            s += 3 * n
    for t in qt:
        if t == leaf:
            s += 9
        elif t in leaf.split():
            s += 5
        elif t in path:
            s += 2
    return s


def name_frequency(nodes):
    """How many projects use each name token. A token almost nobody uses is an
    entity name the query meant literally ('rustls'); a token hundreds share is
    a topic word ('http') and must not act as an identity match."""
    df = {}
    for n in nodes:
        parts = {p for p in re.split(r"[^a-z0-9]+", " ".join(n.get("names", [])).lower()) if p}
        for p in parts:
            df[p] = df.get(p, 0) + 1
    return df


def score_node(n, qt, cat_scores, name_df):
    name = " ".join(n.get("names", [])).lower()
    name_parts = {p for p in re.split(r"[^a-z0-9]+", name) if p}
    crate = (n.get("crate_name") or "").lower()
    desc = (n.get("description") or "").lower()
    desc_words = set(re.split(r"[^a-z0-9]+", desc))

    # Inherit the best-matching category's score: membership of a well-matched
    # category is stronger evidence than any wording in the one-line blurb.
    best_cat, best_path = 0.0, ""
    for p in n.get("category_paths", []):
        sc = cat_scores.get("cat:" + "/".join(p), 0.0)
        if sc >= best_cat:
            best_cat, best_path = sc, " / ".join(p).lower()

    # Tokens the matched category already accounts for must not be paid twice.
    # Without this, "graphql-client" wins a query for "http client" on the word
    # "client", which the category HTTP Client had already credited.
    covered = {t for t in qt if t in best_path} if best_cat > 0 else set()

    s = 0.0
    for t in qt:
        if t and t == crate:
            s += 16           # the query named this crate outright
        elif t in name_parts:
            if t in covered:
                s += 1.0
            else:
                s += 14 if name_df.get(t, 0) <= 3 else 6
        elif t in name:
            s += 0.5          # substring only: weak, this is the "async-*" trap
        if t in desc_words:
            s += 1.5
        elif t in desc:
            s += 0.5

    # A description containing the query phrase verbatim ("an ergonomic HTTP
    # Client") is direct evidence of what the project actually is.
    for ln, ph in phrases(qt):
        if ln > 1 and ph in desc:
            s += 8 * ln
            break

    s += best_cat * 1.4

    if s > 0:
        if n.get("crate_name") or IDENT.get(n["id"]):
            s += 1.0                       # installable, all else equal
        pop, kind = popularity(n)
        if pop:
            # Within one category, adoption is the best quality proxy we have.
            # Downloads span ~9 decades vs ~5 for stars, so scale them apart.
            s += (math.log10(pop + 1) ** 2) * (0.9 if kind == "dl" else 2.0)
    return s


def crate_candidate(n):
    """(name, verified). Verified means the name exists on crates.io and was
    matched by the README link or by the repo name. The low-confidence case is
    a repo publishing several crates where none matches its name, so the most
    downloaded one was taken -- that guess needs a human eye."""
    ident = IDENT.get(n["id"])
    if ident:
        return ident["crate"], ident.get("source") in CONFIDENT
    if n.get("crate_name"):
        return n["crate_name"], True
    repo = n.get("repo")
    if repo and repo.get("name"):
        return repo["name"], False
    return None, False


def human(x):
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if x >= div:
            return "%.1f%s" % (x / div, unit)
    return str(x)


def fmt_node(n, indent="  "):
    out = []
    head = n["name"]
    crate, verified = crate_candidate(n)
    if crate:
        head += "  [%s]" % crate if verified else "  [%s?]" % crate
    pop, kind = popularity(n)
    if pop:
        head += "  %s%s" % (human(pop), " downloads" if kind == "dl" else "*")
    out.append(indent + head)
    if n.get("description"):
        out.append("%s  %s" % (indent, n["description"]))
    out.append("%s  %s" % (indent, n["primary_url"]))
    if crate and verified:
        out.append("%s  cargo add %s" % (indent, crate))
    elif crate:
        out.append("%s  cargo add %s   (UNVERIFIED: guessed, confirm on"
                   " crates.io)" % (indent, crate))
    out.append("%s  README.md:%s | %s" % (
        indent, n["lines"][0], " / ".join(n["category_paths"][0])))
    if n.get("listings", 1) > 1:
        out.append("%s  also listed under: %s" % (indent, "; ".join(
            " / ".join(p) for p in n["category_paths"][1:])))
    return "\n".join(out)


def cmd_ask(query):
    nodes, cats = load()
    qt = tokens(query)
    if not qt:
        sys.exit("empty query")

    cat_scores = {c["id"]: score_category(c, qt) for c in cats}
    name_df = name_frequency(nodes)
    scored = sorted(((score_node(n, qt, cat_scores, name_df), n) for n in nodes),
                    key=lambda x: (-x[0], x[1]["name"]))
    hits = [(s, n) for s, n in scored if s > 0][:8]
    if not hits:
        print("no match for %r" % query)
        return 1

    node_by_id = {n["id"]: n for n in nodes}
    top_cats = [c for c in sorted(cats, key=lambda c: -cat_scores[c["id"]])
                if cat_scores[c["id"]] > 0 and c["members"]][:3]

    print("QUERY  %s" % query)
    print("=" * 72)
    if top_cats:
        print("\nMATCHED CATEGORIES")
        for c in top_cats:
            print("  %-54s %d listed" % (" / ".join(c["path"]), len(c["members"])))
    else:
        # No category matched, so nothing anchors the ranking and the results
        # below are lexical guesses. Say so: the list has its own vocabulary
        # (it says "Encoding", never "serialization") and silently returning
        # near-random projects is worse than reporting the miss.
        print("\nNO CATEGORY MATCHED THIS WORDING")
        print("  Results below are lexical guesses only, not a ranking. This")
        print("  list uses its own vocabulary -- browse it and retry with the")
        print("  term it actually uses:")
        print("      python tools/rustgraph.py cat")

    print("\nBEST MATCHES")
    for _, n in hits[:6]:
        print(fmt_node(n))
        print()

    shown = {n["id"] for _, n in hits[:6]}
    printed_header = False
    for c in top_cats:
        others = [node_by_id[m] for m in c["members"]
                  if m in node_by_id and m not in shown]
        if not others:
            continue
        if not printed_header:
            print("OTHER OPTIONS IN THOSE CATEGORIES")
            printed_header = True
        others.sort(key=lambda n: -((n.get("metrics") or {}).get("github_stars") or 0))
        print("\n  %s" % " / ".join(c["path"]))
        for o in others[:8]:
            crate, verified = crate_candidate(o)
            label = (crate or "-") + ("" if verified else "?")
            print("    %-26s %-18s %s" % (o["name"][:26], label[:18],
                                          (o.get("description") or "")[:66]))

    picks, unverified = [], False
    for _, n in hits[:6]:
        crate, verified = crate_candidate(n)
        if crate and crate not in picks:
            picks.append(crate)
            unverified = unverified or not verified
    if picks:
        print("\nINSTALL")
        print("  cargo add %s" % " ".join(picks[:4]))
        if unverified:
            print("  (names marked ? are guessed from the repo name -- verify"
                  " on crates.io)")
    print("\nEVIDENCE  every line cites README.md; regenerate with"
          " `cargo run --bin index`")
    return 0


def cmd_cat(path):
    nodes, cats = load()
    node_by_id = {n["id"]: n for n in nodes}
    if not path:
        for c in cats:
            if c["depth"] <= 2:
                print("%-52s %4d" % ("/".join(c["path"]), c["subtree_count"]))
        return 0
    want = path.strip("/").lower()
    matches = [c for c in cats if "/".join(c["path"]).lower() == want] or \
              [c for c in cats if want in "/".join(c["path"]).lower()]
    if not matches:
        print("no category matching %r" % path)
        return 1
    for c in matches[:5]:
        print("\n%s  (%d in subtree, %d direct)" % (
            " / ".join(c["path"]), c["subtree_count"], len(c["members"])))
        members = [node_by_id[m] for m in c["members"] if m in node_by_id]
        members.sort(key=lambda n: -((n.get("metrics") or {}).get("github_stars") or 0))
        for n in members:
            crate, verified = crate_candidate(n)
            label = (crate or "-") + ("" if verified else "?")
            print("  %-28s %-20s %s" % (
                n["name"][:28], label[:20], (n.get("description") or "")[:60]))
    return 0


def cmd_node(name):
    nodes, _ = load()
    q = name.lower()
    exact = [n for n in nodes if (n.get("crate_name") or "").lower() == q
             or any(x.lower() == q for x in n.get("names", []))]
    hits = exact or [n for n in nodes if q in " ".join(n.get("names", [])).lower()
                     or q in (n.get("crate_name") or "").lower()]
    if not hits:
        print("no project matching %r" % name)
        return 1
    for n in hits[:5]:
        print(json.dumps(n, indent=1))
    return 0


def strata(adj, roots):
    """Longest-path layering: layer 0 depends on nothing in the index, layer n
    is 1 + its deepest dependency. Cycles clamp so the walk terminates -- the
    crate graph does contain dev-dependency cycles."""
    memo = {}

    def layer(n, stack=()):
        if n in memo:
            return memo[n]
        if n in stack:
            return 0
        deps = adj.get(n, [])
        v = 0 if not deps else 1 + max(layer(d, stack + (n,)) for d in deps)
        memo[n] = v
        return v

    sys.setrecursionlimit(10000)
    for r in roots:
        layer(r)
    out = {}
    for n, v in memo.items():
        out.setdefault(v, []).append(n)
    return out, memo


def dependents_rank(adj):
    """How many indexed projects transitively depend on each node: the
    load-bearing rank. Breaking one of these breaks everything above it."""
    rev = {}
    for src, deps in adj.items():
        for d in deps:
            rev.setdefault(d, set()).add(src)
    rank = {}
    for start in rev:
        seen, frontier = set(), [start]
        while frontier:
            cur = frontier.pop()
            for r in rev.get(cur, ()):
                if r not in seen:
                    seen.add(r)
                    frontier.append(r)
        rank[start] = len(seen)
    return rank


def cmd_dag(write, with_dev=False):
    nodes, _ = load()
    adj = load_deps(kinds=None if with_dev else ("0",))
    if not adj:
        print("index/dep-edges.jsonl missing -- run:")
        print("  python tools/crates_deps.py <db-dump.tar.gz>")
        return 1
    name_of = {n["id"]: n["name"] for n in nodes}
    involved = set(adj) | {d for v in adj.values() for d in v}
    layers, layer_of = strata(adj, sorted(involved))
    rank = dependents_rank(adj)

    print("dependency DAG over indexed projects")
    print("  nodes in graph  %d of %d" % (len(involved), len(nodes)))
    print("  edges           %d" % sum(len(v) for v in adj.values()))
    print("\nstrata (all dependencies satisfied by lower strata):")
    for k in sorted(layers):
        names = sorted(name_of.get(n, n) for n in layers[k])
        print("  L%-2d %4d  %s%s" % (k, len(names), ", ".join(names[:6]),
                                     " ..." if len(names) > 6 else ""))
    print("\nmost load-bearing (transitive dependents inside the index):")
    for nid, c in sorted(rank.items(), key=lambda kv: (-kv[1], kv[0]))[:15]:
        crate = (IDENT.get(nid) or {}).get("crate", "")
        print("  %4d  %-24s %s" % (c, name_of.get(nid, nid)[:24], crate))
    if write:
        DAG.write_text(json.dumps({
            "adjacency": adj,
            "strata": {str(k): sorted(v) for k, v in sorted(layers.items())},
            "layer_of": layer_of,
            "dependents_rank": dict(sorted(rank.items(), key=lambda kv: -kv[1])),
        }, indent=1, sort_keys=True), encoding="utf-8")
        print("\nwrote %s" % DAG)
    return 0


def cmd_stats():
    nodes, cats = load()
    print("nodes          %d" % len(nodes))
    print("  with crate   %d" % sum(1 for n in nodes if n.get("crate_name")))
    print("  multi-listed %d" % sum(1 for n in nodes if n.get("listings", 1) > 1))
    print("  enriched     %d" % sum(1 for n in nodes if n.get("metrics")))
    print("categories     %d" % len(cats))
    print("\nlargest categories:")
    for c in sorted(cats, key=lambda c: -len(c["members"]))[:12]:
        print("  %-52s %3d" % ("/".join(c["path"]), len(c["members"])))
    return 0


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 0
    cmd, rest = argv[1], argv[2:]
    if cmd == "ask":
        return cmd_ask(" ".join(rest))
    if cmd == "cat":
        return cmd_cat(" ".join(rest))
    if cmd == "node":
        return cmd_node(" ".join(rest))
    if cmd == "dag":
        return cmd_dag("--write" in rest, "--with-dev" in rest)
    if cmd == "stats":
        return cmd_stats()
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
