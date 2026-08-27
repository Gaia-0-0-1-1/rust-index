#!/usr/bin/env python3
"""rustgraph.py -- the agent-facing query surface over index/.

Built on the artifacts `cargo run --bin index` produces. Stdlib only, no network.

    ask <query...>
        Rank projects against a need, expand to the sibling alternatives, and
        emit a context pack: the brief an agent gets before choosing a crate.
    cat [path]
        List a category and its members. Members of one category are mutual
        alternatives.
    dag [--write] [--with-dev]
        Dependency strata and load-bearing rank. Needs crates_deps.py first.
    node <name|crate>
        Everything known about one project.
    recipe [name]
        Authored stacks per archetype, each crate resolved against the index.
    stack <name|crate>
        What a project is demonstrably built alongside, from the dep graph.
    selftest
        Run tools/selftest.json; non-zero exit on any mismatch.
    stats
        Index shape.

    python tools/rustgraph.py ask "async http client with rustls"
    python tools/rustgraph.py cat "Libraries/Command-line/Argument parsing"
    python tools/rustgraph.py node reqwest

Ranking, in three rules. The taxonomy is matched first, because in a curated
list it carries more signal than the prose -- on names alone "async" pulls in
async-std, async-dropper and async-graphql by pure string overlap. A category is
reachable through the vocabulary its members are *described* with, aggregated
over its subtree, so "serialization" finds Encoding although that name never says
the word -- and through their crates.io keywords and categories, which is how
"REST API" reaches HTTP Server in a list that only ever writes "HTTP". Adoption
then orders results within the categories that matched, and is damped outside
them: counted globally, clap's billion downloads win "terminal ui" on fame.
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
VOCABULARY = ROOT / "index" / "crate-vocabulary.json"
RECIPES = ROOT / "tools" / "recipes.json"
SELFTEST = ROOT / "tools" / "selftest.json"

# Filled by load(). Absent until tools/crates_deps.py has run.
IDENT = {}
DL = {}
VOCAB = {}
# dep-edges.jsonl is a megabyte; stack and ask both want it more than once.
_DEPS_CACHE = {}
_AFFINITY_CACHE = {}
CONFIDENT = {"readme", "repo-exact", "repo-alias"}

STOP = {
    "a", "an", "and", "for", "the", "with", "to", "in", "of", "on", "is", "it",
    "i", "want", "need", "using", "use", "how", "do", "build", "make", "my",
    "that", "this", "be", "can", "some", "library", "crate", "rust", "lib",
}


def tokens(s):
    return [t for t in re.split(r"[^a-z0-9+#.]+", s.lower()) if t and t not in STOP]


def load():
    global IDENT, DL, VOCAB
    if not NODES.exists():
        sys.exit("index/nodes.jsonl missing -- run: cargo run --bin index")
    nodes = [json.loads(l) for l in NODES.read_text(encoding="utf-8").splitlines() if l.strip()]
    cats = json.loads(CATEGORIES.read_text(encoding="utf-8")) if CATEGORIES.exists() else []
    if IDENTITY.exists():
        IDENT = json.loads(IDENTITY.read_text(encoding="utf-8"))
    if DOWNLOADS.exists():
        DL = json.loads(DOWNLOADS.read_text(encoding="utf-8"))
    if VOCABULARY.exists():
        VOCAB = json.loads(VOCABULARY.read_text(encoding="utf-8"))
    return nodes, cats


def load_deps(kinds=("0",)):
    """from -> [deps]. 'from depends on to', matching the edge direction.

    Normal dependencies only by default. Dev-dependencies (kind 2) are not
    runtime edges and they close cycles -- serde dev-depends on serde_json,
    which depends on serde -- which fuses the graph into one component and
    makes every transitive-dependent count saturate at the same number."""
    if kinds in _DEPS_CACHE:
        return _DEPS_CACHE[kinds]
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
    out = {k: sorted(v) for k, v in adj.items()}
    _DEPS_CACHE[kinds] = out
    return out


def dep_split():
    """src -> (required, optional). Feature-gated deps are not the same promise.

    "axum requires tokio" is a constraint on the caller: pick axum and the runtime
    is decided. "lettre optionally depends on tokio" means the opposite -- it
    adapts to whichever runtime is already there. Collapsing the two, as a plain
    dependency list does, loses exactly the fact an agent needs."""
    if "split" in _DEPS_CACHE:
        return _DEPS_CACHE["split"]
    req, opt = {}, {}
    if DEP_EDGES.exists():
        for line in DEP_EDGES.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if e.get("dep_kind") != "0":
                continue
            bucket = opt if e.get("optional") else req
            bucket.setdefault(e["from"], set()).add(e["to"])
    out = (req, opt)
    _DEPS_CACHE["split"] = out
    return out


def runtime_note(node_id):
    """The single most consequential thing a crate can require in async Rust.

    Three-way, because Cargo's optional flag alone would mislead: axum gates
    tokio behind a feature, but `default` turns that feature on, so reporting it
    as merely optional tells an agent it has a choice it does not have. Only a
    dependency that is optional *and* outside the default closure is opt-in."""
    req, opt = dep_split()
    required = req.get(node_id, set())
    optional = opt.get(node_id, set())
    defaults = set((VOCAB.get(node_id) or {}).get("default_features") or ())

    hits = []
    for rt in ("tokio", "async-std"):
        rid = next((i for i in required | optional
                    if (IDENT.get(i) or {}).get("crate") == rt), None)
        if rid is None:
            continue
        if rid in required:
            hits.append("requires %s" % rt)
        elif rt in defaults:
            hits.append("requires %s (on by default; disable default features"
                        " to avoid it)" % rt)
        else:
            hits.append("%s available behind a feature" % rt)
    return ", ".join(hits)


def affinity(min_support=4):
    """Which projects genuinely get used together, from the dependency graph.

    Two crates listed in the same Cargo.toml co-occur. Raw counts are useless --
    the top pairs are serde_json+tokio, clap+serde_json, everything+everything,
    because those appear in nearly every manifest. Lift corrects for that:

        lift(a,b) = P(a,b) / (P(a) * P(b))

    A pair scores high only when it co-occurs more than its two members' own
    ubiquity would predict. That surfaces tonic+prost, quinn+rustls, zstd+bzip2 --
    real affinities rather than a popularity ranking in disguise. Support >= 4
    keeps a single shared manifest from inventing a relationship."""
    if min_support in _AFFINITY_CACHE:
        return _AFFINITY_CACHE[min_support]
    deps = {}
    for src, ds in load_deps().items():
        deps[src] = set(ds)
    pair, freq = {}, {}
    for ds in deps.values():
        members = sorted(ds)
        for d in members:
            freq[d] = freq.get(d, 0) + 1
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                pair[(a, b)] = pair.get((a, b), 0) + 1

    total = len(deps) or 1
    out = {}
    for (a, b), count in pair.items():
        if count < min_support:
            continue
        score = (count * total) / (freq[a] * freq[b])
        out.setdefault(a, []).append((score, count, b))
        out.setdefault(b, []).append((score, count, a))
    for k in out:
        out[k].sort(key=lambda x: (-x[0], x[2]))
    _AFFINITY_CACHE[min_support] = out
    return out


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


def subtree_map(cats):
    """category id -> every member of it and of its descendants.

    `members` on a Category is direct membership only, which is nearly empty for
    an interior node like Libraries/Encoding while its children hold everything.
    Anything reasoning about a category as a group wants the subtree."""
    out = {}
    for c in cats:
        prefix = c["id"] + "/"
        members = set(c["members"])
        for other in cats:
            if other["id"].startswith(prefix):
                members.update(other["members"])
        out[c["id"]] = sorted(members)
    return out


def term_match(q, term):
    """Same morphological root, without pulling in a stemmer.

    Both tests need 8 characters of agreement, which is not fussiness: at 6,
    "serial" (as in serial port) matches "serialization" and drags Embedded and
    Filesystem into a query about encoding. Eight still catches
    serialization/deserialization by containment and serialization/serializer by
    shared prefix, while keeping the shorter, more ambiguous roots apart."""
    if q == term:
        return True
    if min(len(q), len(term)) < 8:
        return False
    if q in term or term in q:
        return True
    n = 0
    for a, b in zip(q, term):
        if a != b:
            break
        n += 1
    return n >= 8


def category_terms(nodes, cats):
    """Distinctive words from each category's members' descriptions, by TF-IDF.

    The list's category names are a tiny vocabulary; what its members are
    *described* as is a much larger one. This is what lets "serialization" reach
    Encoding, whose members talk about serializers and deserialization while the
    category name never says the word. Derived, so it stays true as the list
    changes -- unlike a hand-maintained synonym table.

    Two details matter. Terms are gathered over each category's whole subtree,
    not its direct members: "Encoding" itself lists almost nothing, while
    Encoding/JSON, /YAML, /CBOR and the rest collectively supply the vocabulary,
    and the subtree is the real community. And a term counts by how many members
    mention it, not how often: raw TF-IDF prizes rarity, which let a single
    passing "serializing" in Geospatial outrank the whole Encoding subtree."""
    node_by_id = {n["id"]: n for n in nodes}
    subtree = subtree_map(cats)
    words_of = {}
    for n in nodes:
        words = {
            w for w in re.split(r"[^a-z0-9]+", (n.get("description") or "").lower())
            if len(w) >= 4 and w not in STOP
        }
        # Fold in the crate's own keywords and crates.io categories. A ten-word
        # blurb is a thin basis for a topic; these are curated, and they carry
        # words the README never uses -- "rest", "orm", "persistence". They need
        # no special weight: scoring already rewards a term many members share,
        # and IDF discounts the ubiquitous ones like command-line-utilities.
        vocab = VOCAB.get(n["id"])
        if vocab:
            words.update(vocab.get("keywords", ()))
            words.update(vocab.get("categories", ()))
        words_of[n["id"]] = words

    docs, df = {}, {}
    for c in cats:
        members = [m for m in subtree.get(c["id"], ()) if m in node_by_id]
        if not members:
            continue
        mentions = {}
        for m in members:
            for w in words_of.get(m, ()):
                mentions[w] = mentions.get(w, 0) + 1
        if mentions:
            docs[c["id"]] = (mentions, len(members))
            for w in mentions:
                df[w] = df.get(w, 0) + 1

    total = len(docs) or 1
    profiles = {}
    for cid, (mentions, size) in docs.items():
        # Consistency (what share of members say it) times evidence (how many).
        # Share alone lets a one-member leaf outrank the whole Encoding subtree;
        # count alone lets a big category win on a term few of its members use.
        scored = {
            w: (math.sqrt(cnt / size) * math.log(1 + cnt)
                * math.log(total / (1 + df[w])) * 4)
            for w, cnt in mentions.items()
            if cnt >= 2
        }
        profiles[cid] = dict(sorted(scored.items(), key=lambda kv: -kv[1])[:30])
    return profiles


def score_category(cat, qt, profile=None):
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
        # Short tokens are too risky for term_match but are exactly how the
        # acronym categories get named: "terminal ui" has to reach TUI, and
        # "ui" only ever appears there as the tail of the acronym itself.
        if 2 <= len(t) <= 4 and len(leaf) <= 6 and t != leaf:
            if leaf.startswith(t) or leaf.endswith(t):
                s += 6
    # Fall back to how the members are described. Weighted below any name match,
    # so a category that says the word still beats one that merely implies it.
    if profile:
        for t in qt:
            best = 0.0
            for term, weight in profile.items():
                if term_match(t, term):
                    best = max(best, weight)
            s += min(best, 8.0) * 0.9
    return s


def keyword_frequency():
    """How many crates hold each crates.io keyword.

    The same insight the name matching uses, applied to curated vocabulary. A
    keyword held by one crate is nearly an identifier -- "scraping" belongs to
    exactly one project here -- while "web" is held by forty and says little.
    A flat bonus treats those as equal, which is why a query for web scraping
    returned axum: "web" matched a broad category, and the one crate that
    actually says "scraping" could not outweigh axum's downloads."""
    if "kwdf" in _AFFINITY_CACHE:
        return _AFFINITY_CACHE["kwdf"]
    df = {}
    for v in VOCAB.values():
        for k in set(v.get("keywords") or ()) | set(v.get("categories") or ()):
            df[k] = df.get(k, 0) + 1
    _AFFINITY_CACHE["kwdf"] = df
    return df


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


def score_node(n, qt, cat_scores, name_df, anchored=None):
    name = " ".join(n.get("names", [])).lower()
    name_parts = {p for p in re.split(r"[^a-z0-9]+", name) if p}
    crate = (n.get("crate_name") or "").lower()
    desc = (n.get("description") or "").lower()
    desc_words = set(re.split(r"[^a-z0-9]+", desc))

    # Inherit the best-matching category's score: membership of a well-matched
    # category is stronger evidence than any wording in the one-line blurb.
    # Every ancestor counts, not just the exact path: a query that matches
    # "Encoding" must lift serde_json in Encoding/JSON, or matching the parent
    # category surfaces the category and none of the projects inside it.
    best_cat, best_path = 0.0, ""
    for p in n.get("category_paths", []):
        for depth in range(len(p), 0, -1):
            sc = cat_scores.get("cat:" + "/".join(p[:depth]), 0.0)
            if sc >= best_cat:
                best_cat, best_path = sc, " / ".join(p[:depth]).lower()

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

    # The crate's own keywords and crates.io categories. Curated rather than
    # prose, so an exact hit is real evidence -- but kept below a category match,
    # since a keyword is one author's word for their crate, not a consensus.
    vocab = VOCAB.get(n["id"])
    if vocab:
        own = set(vocab.get("keywords", ())) | set(vocab.get("categories", ()))
        kwdf = keyword_frequency()
        for t in qt:
            if t in own:
                held = kwdf.get(t, 0)
                s += 20 if held <= 3 else (9 if held <= 12 else 3)
            elif any(term_match(t, k) for k in own):
                s += 2

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
            # Adoption decides between siblings, and must not decide between
            # topics. Inside a category the query actually matched, weight it
            # hard -- that is what puts reqwest above an obscure HTTP client.
            # Outside one, damp it to a whisper, or clap's billion downloads
            # win "terminal ui" purely on fame.
            scale = 0.9 if kind == "dl" else 2.0
            if not anchored:
                s += (math.log10(pop + 1) ** 2) * scale
            elif best_path in anchored:
                s += (math.log10(pop + 1) ** 2) * scale
            else:
                s += math.log10(pop + 1) * 0.5
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


def rank(nodes, cats, query):
    """The whole ranking, with nothing printed, so selftest exercises exactly
    what ask does. Returns (hits, top_cats, subtree)."""
    qt = tokens(query)
    if not qt:
        return [], [], {}

    profiles = category_terms(nodes, cats)
    cat_scores = {
        c["id"]: score_category(c, qt, profiles.get(c["id"])) for c in cats}
    name_df = name_frequency(nodes)
    subtree = subtree_map(cats)

    # A real query anchors on a category at 7 or more; noise scrapes 2 off some
    # incidental substring ("flux" finding InfluxDB). Below the floor there is
    # no anchor, so say so rather than dressing a guess up as a ranking.
    ANCHOR = 4.0
    # A depth-1 category is a whole section of the list ("Applications", 631
    # projects). It is never an actionable answer, and it matches broadly
    # precisely because it contains everything.
    def shown(c):
        return (cat_scores[c["id"]] >= ANCHOR and c["depth"] > 1
                and subtree.get(c["id"]))

    by_score = sorted(cats, key=lambda c: -cat_scores[c["id"]])
    ranked = [c for c in by_score if shown(c)]
    # Keep only what is close to the best match. Broad categories saturate at an
    # identical score -- a query for terminal tables tied Audio and Music with
    # File manager, since some of each are terminal apps -- and showing that tail
    # implies a relevance the tie does not support.
    # 0.75 is measured, not guessed: across sample queries the genuinely related
    # neighbours land at 75% of the best (web framework -> Frameworks and
    # Runners, Backend) while the saturated ties land at 69%.
    top_cats = [c for c in ranked
                if cat_scores[c["id"]] >= 0.75 * cat_scores[ranked[0]["id"]]][:3] \
        if ranked else []
    # Fame counts fully only inside the categories actually shown. Anchoring on
    # every category above the floor is too generous: "terminal ui" lifts
    # Command-line enough to clear it, and clap then wins on downloads from a
    # category the user was never offered.
    anchored = {" / ".join(c["path"]).lower() for c in top_cats}

    scored = sorted(
        ((score_node(n, qt, cat_scores, name_df, anchored), n) for n in nodes),
        key=lambda x: (-x[0], x[1]["name"]))
    return [(sc, n) for sc, n in scored if sc > 0][:8], top_cats, subtree


def cmd_ask(query):
    nodes, cats = load()
    if not tokens(query):
        sys.exit("empty query")
    hits, top_cats, subtree = rank(nodes, cats, query)
    node_by_id = {n["id"]: n for n in nodes}
    if not hits:
        print("no match for %r" % query)
        return 1

    print("QUERY  %s" % query)
    print("=" * 72)
    if top_cats:
        print("\nMATCHED CATEGORIES")
        for c in top_cats:
            size = len(subtree.get(c["id"], []))
            print("  %-54s %d listed" % (" / ".join(c["path"]), size))
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
        others = [node_by_id[m] for m in subtree.get(c["id"], [])
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

    # What the leading answer is actually built with. Picking a crate is only
    # half the question an agent has; the other half is what comes with it, and
    # that is the one thing only the dependency graph knows.
    aff = affinity()
    lead = hits[0][1]
    partners = aff.get(lead["id"], [])
    if partners:
        lead_crate, _lv = crate_candidate(lead)
        rt = runtime_note(lead["id"])
        if rt:
            print("RUNTIME  %s %s" % (lead_crate or lead["name"], rt))
        print("COMMONLY USED WITH %s" % (lead_crate or lead["name"]))
        for score, count, other in partners[:5]:
            o = node_by_id.get(other)
            if not o:
                continue
            oc, _ov = crate_candidate(o)
            blurb = (o.get("description") or "")[:42]
            print("    %-22s x%-5.1f  %s" % (oc or o["name"], score, blurb))
        print()

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
        }, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print("\nwrote %s" % DAG)
    return 0


def cmd_recipe(name):
    """Authored stacks per archetype, every crate resolved against the index.

    A recipe is the one hand-written thing here, so it is also the one thing that
    can quietly rot. Resolving each crate on every run means a name that is stale,
    mistyped or invented shows up as NOT IN THIS LIST instead of as advice."""
    if not RECIPES.exists():
        print("tools/recipes.json missing")
        return 1
    data = json.loads(RECIPES.read_text(encoding="utf-8"))["recipes"]
    nodes, _ = load()
    by_crate = {}
    for n in nodes:
        crate, _verified = crate_candidate(n)
        if crate:
            by_crate.setdefault(crate.lower(), n)

    if not name:
        print("recipes (python tools/rustgraph.py recipe <name>)\n")
        for r in data:
            print("  %-14s %s" % (r["name"], r["title"]))
            print("  %-14s %s" % ("", r["when"]))
        return 0

    r = next((x for x in data if x["name"] == name.strip().lower()), None)
    if not r:
        print("no recipe %r. Known: %s" % (name, ", ".join(x["name"] for x in data)))
        return 1

    print("%s -- %s" % (r["name"], r["title"]))
    print(r["when"])
    print("=" * 72)
    covered, missing = [], []
    for step in r["steps"]:
        n = by_crate.get(step["crate"].lower())
        print("\n  %s" % step["role"])
        if n:
            pop, kind = popularity(n)
            tag = "  %s%s" % (human(pop), " downloads" if kind == "dl" else "*") if pop else ""
            print("    %s%s" % (step["crate"], tag))
            where = " / ".join(n["category_paths"][0])
            print("    README.md:%s | %s" % (n["lines"][0], where))
            if step["crate"] not in covered:
                covered.append(step["crate"])
        else:
            print("    %s   NOT IN THIS LIST" % step["crate"])
            if step["crate"] not in missing:
                missing.append(step["crate"])
        print("    %s" % step["why"])

    if covered:
        print("\nINSTALL")
        print("  cargo add %s" % " ".join(covered))
    if missing:
        print("\nNOT COVERED BY THIS LIST: %s" % ", ".join(missing))
        print("  Real stacks need these, but awesome-rust does not list them, so")
        print("  nothing here can vouch for them. Check crates.io directly.")
    return 0


def cmd_selftest():
    """Run tools/selftest.json. Non-zero exit on any mismatch.

    Two things go unchecked without this. The ranking's behaviour lived only in
    whatever queries someone last happened to type, and CI ran no Python at all,
    so a broken tool or a rotted recipe stayed green."""
    if not SELFTEST.exists():
        print("tools/selftest.json missing")
        return 1
    spec = json.loads(SELFTEST.read_text(encoding="utf-8"))
    nodes, cats = load()
    failures = []

    for case in spec.get("queries", []):
        hits, top_cats, _ = rank(nodes, cats, case["q"])
        got = None
        if hits:
            got, _v = crate_candidate(hits[0][1])
        ok = got is not None and got.lower() == case["top"].lower()
        if not ok:
            failures.append("query %r: expected %s, got %s"
                            % (case["q"], case["top"], got or "no hit"))
        print("  %-4s %-32s -> %s" % ("ok" if ok else "FAIL", case["q"], got))

    for case in spec.get("no_anchor", []):
        _hits, top_cats, _ = rank(nodes, cats, case["q"])
        ok = not top_cats
        if not ok:
            failures.append("no_anchor %r: anchored on %s"
                            % (case["q"], " / ".join(top_cats[0]["path"])))
        verdict = "no anchor" if ok else "anchored"
        mark = "ok" if ok else "FAIL"
        print("  %-4s %-32s -> %s" % (mark, case["q"], verdict))

    if spec.get("recipes") and RECIPES.exists():
        data = json.loads(RECIPES.read_text(encoding="utf-8"))["recipes"]
        by_crate = {}
        for n in nodes:
            crate, _v = crate_candidate(n)
            if crate:
                by_crate.setdefault(crate.lower(), n)
        for case in spec["recipes"]:
            r = next((x for x in data if x["name"] == case["name"]), None)
            if not r:
                failures.append("recipe %s: missing from recipes.json" % case["name"])
                print("  FAIL %-32s -> missing" % case["name"])
                continue
            resolved, uncovered = 0, []
            for step in r["steps"]:
                if step["crate"].lower() in by_crate:
                    resolved += 1
                elif step["crate"] not in uncovered:
                    uncovered.append(step["crate"])
            ok = resolved == case["resolved"] and uncovered == case["uncovered"]
            if not ok:
                want = "%d resolved / %s uncovered" % (
                    case["resolved"], case["uncovered"])
                got = "%d / %s" % (resolved, uncovered)
                failures.append("recipe %s: expected %s, got %s"
                                % (case["name"], want, got))
            mark = "ok" if ok else "FAIL"
            summary = "%d resolved, uncovered %s" % (
                resolved, uncovered or "none")
            print("  %-4s recipe %-25s -> %s" % (mark, case["name"], summary))

    if spec.get("affinity"):
        aff = affinity()
        by_crate = {}
        for n in nodes:
            c, _v = crate_candidate(n)
            if c:
                by_crate.setdefault(c.lower(), n)
        for case in spec["affinity"]:
            node = by_crate.get(case["crate"].lower())
            partners = aff.get(node["id"], []) if node else []
            top = None
            if partners:
                o = next((x for x in nodes if x["id"] == partners[0][2]), None)
                if o:
                    top, _tv = crate_candidate(o)
            ok = top is not None and top.lower() == case["expect"].lower()
            if not ok:
                failures.append("affinity %s: expected %s, got %s"
                                % (case["crate"], case["expect"], top or "none"))
            mark = "ok" if ok else "FAIL"
            label = "affinity " + case["crate"]
            print("  %-4s %-32s -> %s" % (mark, label, top))

    if spec.get("runtime"):
        by_crate = {}
        for n in nodes:
            c, _v = crate_candidate(n)
            if c:
                by_crate.setdefault(c.lower(), n)
        for case in spec["runtime"]:
            node = by_crate.get(case["crate"].lower())
            got = runtime_note(node["id"]) if node else ""
            ok = case["expect"].lower() in got.lower()
            if not ok:
                failures.append("runtime %s: expected %r in %r"
                                % (case["crate"], case["expect"], got))
            mark = "ok" if ok else "FAIL"
            label = "runtime " + case["crate"]
            print("  %-4s %-32s -> %s" % (mark, label, got[:34] or "none"))

    print()
    if failures:
        print("SELFTEST FAILED (%d)" % len(failures))
        for f in failures:
            print("  %s" % f)
        print("\nIf the README or the crates.io dump legitimately changed an")
        print("answer, update tools/selftest.json in the same commit.")
        return 1
    print("selftest OK")
    return 0


def cmd_stack(name):
    """What a project is actually built alongside, from the dependency graph.

    The hand-authored recipes say what a stack *should* be. This says what people
    demonstrably do, which is the same question answered by evidence instead of
    by opinion -- and it covers every indexed crate, not the seven archetypes
    someone got round to writing."""
    if not name:
        print("usage: stack <name|crate>")
        return 1
    nodes, _cats = load()
    q = name.strip().lower()
    node_by_id = {n["id"]: n for n in nodes}

    def crate_of(n):
        c, _v = crate_candidate(n)
        return (c or "").lower()

    target = next((n for n in nodes if crate_of(n) == q), None) \
        or next((n for n in nodes if any(x.lower() == q for x in n.get("names", []))), None)
    if not target:
        print("no project matching %r" % name)
        return 1

    aff = affinity()
    partners = aff.get(target["id"], [])
    deps = set(load_deps().get(target["id"], ()))

    crate, _v = crate_candidate(target)
    print("%s -- what it is built with" % (crate or target["name"]))
    print("%s" % (target.get("description") or ""))
    rt = runtime_note(target["id"])
    if rt:
        print("RUNTIME: %s" % rt)
    print("=" * 72)

    if not partners and not deps:
        print("\nNo dependency evidence for this project in the index.")
        print("Either it is not on crates.io, or nothing else here shares a")
        print("manifest with it. Try: rustgraph.py recipe")
        return 0

    if partners:
        print("\nCOMMONLY USED ALONGSIDE  (lift over chance, support in manifests)")
        for score, count, other in partners[:10]:
            o = node_by_id.get(other)
            if not o:
                continue
            oc, _ov = crate_candidate(o)
            blurb = (o.get("description") or "")[:34]
            row = "  %-22s x%-5.1f  in %2d manifests   %s"
            print(row % (oc or o["name"], score, count, blurb))

    req, opt = dep_split()
    groups = (
        ("REQUIRES", req.get(target["id"], set())),
        ("OPTIONAL, BEHIND A FEATURE", opt.get(target["id"], set())),
    )
    for label, group in groups:
        group = {d for d in group if d in node_by_id}
        if not group:
            continue
        print("\n%s  (of what this list carries)" % label)
        for d in sorted(group):
            o = node_by_id[d]
            oc, _ov = crate_candidate(o)
            blurb = (o.get("description") or "")[:44]
            print("  %-22s %s" % (oc or o["name"], blurb))

    picks = []
    for _s, _c, other in partners[:4]:
        o = node_by_id.get(other)
        if o:
            oc, ver = crate_candidate(o)
            if oc and ver and oc not in picks:
                picks.append(oc)
    if crate:
        picks.insert(0, crate)
    if picks:
        print("\nINSTALL")
        print("  cargo add %s" % " ".join(picks[:5]))
    manifests = len(load_deps())
    note = "\nEvidence: co-occurrence across %d dependency manifests here."
    print(note % manifests)
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
    if cmd == "recipe":
        return cmd_recipe(" ".join(rest))
    if cmd == "stack":
        return cmd_stack(" ".join(rest))
    if cmd == "selftest":
        return cmd_selftest()
    if cmd == "stats":
        return cmd_stats()
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
