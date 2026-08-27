#!/usr/bin/env python3
"""rustgraph.py -- the agent-facing query surface over index/.

Built on the artifacts `cargo run --bin index` produces. Stdlib only, no network.

  ask <query...>     rank projects against a need, expand to the sibling
                     alternatives, and emit a context pack: the brief an agent
                     gets before choosing a crate
  cat [path]         list a category and its members (members of one category
                     are mutual alternatives)
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

STOP = {
    "a", "an", "and", "for", "the", "with", "to", "in", "of", "on", "is", "it",
    "i", "want", "need", "using", "use", "how", "do", "build", "make", "my",
    "that", "this", "be", "can", "some", "library", "crate", "rust", "lib",
}


def tokens(s):
    return [t for t in re.split(r"[^a-z0-9+#.]+", s.lower()) if t and t not in STOP]


def load():
    if not NODES.exists():
        sys.exit("index/nodes.jsonl missing -- run: cargo run --bin index")
    nodes = [json.loads(l) for l in NODES.read_text(encoding="utf-8").splitlines() if l.strip()]
    cats = json.loads(CATEGORIES.read_text(encoding="utf-8")) if CATEGORIES.exists() else []
    return nodes, cats


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
        if n.get("crate_name"):
            s += 1.0                       # installable, all else equal
        stars = (n.get("metrics") or {}).get("github_stars")
        if stars:
            # Within one category, adoption is the best quality proxy we have.
            s += (math.log10(stars + 1) ** 2) * 2.0
    return s


def crate_candidate(n):
    """(name, verified). Unverified means the README carried no crates.io link
    and we are guessing from the repo name -- the agent must confirm."""
    if n.get("crate_name"):
        return n["crate_name"], True
    repo = n.get("repo")
    if repo and repo.get("name"):
        return repo["name"], False
    return None, False


def fmt_node(n, indent="  "):
    out = []
    head = n["name"]
    crate, verified = crate_candidate(n)
    if crate:
        head += "  [%s]" % crate if verified else "  [%s?]" % crate
    stars = (n.get("metrics") or {}).get("github_stars")
    if stars:
        head += "  %d*" % stars
    out.append(indent + head)
    if n.get("description"):
        out.append("%s  %s" % (indent, n["description"]))
    out.append("%s  %s" % (indent, n["primary_url"]))
    if crate and verified:
        out.append("%s  cargo add %s" % (indent, crate))
    elif crate:
        out.append("%s  cargo add %s   (UNVERIFIED: no crates.io link in README)"
                   % (indent, crate))
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
    if cmd == "stats":
        return cmd_stats()
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
