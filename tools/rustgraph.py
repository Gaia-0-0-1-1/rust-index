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
    features <crate> [what]
        Cargo features: what is on by default, what to opt into, and the
        Cargo.toml line for it.
    example <crate>
        The crate's own starting example, from its README. 44% coverage.
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
Adoption orders results but does not decide them on its own -- name, keyword and
description matching still reorder within a category, which is why reqwest can
sit above hyper despite fewer downloads.
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
# A crate name used by at most this many projects' text counts as an
# identifier; above it the name is a dictionary word and matching it is
# a coincidence. Swept against the evaluation set, not guessed.
IDENT_DF_MAX = 30
# How hard adoption pushes inside a matched category. Swept against the
# evaluation set: too high and fame beats intent (core-foundation, a
# macOS-only binding, won "cross platform gui" over iced, which is
# literally described as one); too low and obscure crates win on wording.
POP_WEIGHT = 0.55
# Years of silence before a result is flagged as dormant. Display only:
# discounting adoption by staleness was swept and traded misses rather than
# fixing them, so the fact is reported and not ranked on.
STALE_YEARS = 3

STOP = {
    "a", "an", "and", "for", "the", "with", "to", "in", "of", "on", "is", "it",
    "i", "want", "need", "using", "use", "how", "do", "build", "make", "my",
    "that", "this", "be", "can", "some", "library", "crate", "rust", "lib",
}


MAX_TOKENS = 32


def tokens(s):
    """Query tokens, bounded. Scoring is linear in token count across 2,057
    scored objects, so an unbounded paste is a denial of service on itself."""
    raw = re.split(r"[^a-z0-9+#.]+", s.lower())
    out = [t for t in raw if len(t) > 1 and t not in STOP]
    return out[:MAX_TOKENS]


def stem(w):
    """Fold the endings that split a query from an index entry.

    structopt is described as parsing a "command line argument"; asking for
    "command line arguments" matched neither its description nor its category,
    and a line-editing crate won on a phrase bonus. British spelling splits the
    same way -- "serialisation" found nothing while "serialization" worked. This
    is not a stemmer, it is the four endings that actually cost hits."""
    if len(w) < 4:
        return w
    if w.endswith("isation"):
        return w[:-7] + "ization"
    if w.endswith("ise"):
        return w[:-3] + "ize"
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    for suf in ("sses", "ches", "shes", "xes", "zes"):
        if w.endswith(suf):
            return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and not w.endswith("us"):
        return w[:-1]
    return w


def expand(words):
    """A matching set that holds each word and its folded form."""
    out = set()
    for w in words:
        if w:
            out.add(w)
            out.add(stem(w))
    return out


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


PROVENANCE = ROOT / "index" / "crates-provenance.json"


def snapshot_year():
    """The dump's year, not today's. Ranking must not drift with the clock, or
    a regenerated index stops reproducing byte-for-byte."""
    if "snapyear" in _AFFINITY_CACHE:
        return _AFFINITY_CACHE["snapyear"]
    year = 0
    if PROVENANCE.exists():
        try:
            snap = json.loads(PROVENANCE.read_text(encoding="utf-8"))
            year = int(str(snap.get("dump_snapshot") or "")[:4])
        except (ValueError, TypeError):
            year = 0
    _AFFINITY_CACHE["snapyear"] = year
    return year


def staleness(node_id):
    """Years since the crate last published, against the dump snapshot.

    Downloads are cumulative and never decay, so a crate that was popular and
    then stopped outranks its living replacement forever. rust-crypto last
    shipped in 2016 and is RUSTSEC-flagged. Damping adoption by this was swept
    and traded misses rather than fixing them, so it is shown, not ranked on --
    an agent can weigh "last published 2016" for itself."""
    year = snapshot_year()
    when = (VOCAB.get(node_id) or {}).get("last_published") or ""
    if not year or len(when) < 4:
        return 0.0
    try:
        age = year - int(when[:4])
    except ValueError:
        return 0.0
    return max(age, 0)


def popularity(n):
    """Downloads if the dump has run, else GitHub stars, else nothing."""
    d = DL.get(n["id"])
    if d:
        return d, "dl"
    stars = (n.get("metrics") or {}).get("github_stars")
    return (stars, "star") if stars else (None, None)


MAX_PHRASE = 4


def phrases(qt):
    """Longest contiguous token runs first: 'http client' before 'http'.

    Capped at MAX_PHRASE. Uncapped this is O(n^2) in query length, and every
    phrase is then scored against 355 categories and 1,702 nodes -- so a pasted
    paragraph, which is exactly what an agent does, hung the tool: 158 words
    took 17 seconds and 237 words never returned."""
    for n in range(min(len(qt), MAX_PHRASE), 0, -1):
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
        words = expand(
            w for w in re.split(r"[^a-z0-9]+", (n.get("description") or "").lower())
            if len(w) >= 4 and w not in STOP)
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
        elif t in expand(leaf.split()) or stem(t) in expand(leaf.split()):
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


def corpus_frequency(nodes):
    """How many projects use each word anywhere in their text.

    Used to tell a crate name that is an identifier from one that is a dictionary
    word. "wgpu" appears in five projects' text and "time" in fifty-three, so a
    query token equal to a crate's name is near-conclusive in the first case and
    almost meaningless in the second -- a flat bonus for both makes the crate
    `time` beat chrono for "date and time"."""
    if "corpusdf" in _AFFINITY_CACHE:
        return _AFFINITY_CACHE["corpusdf"]
    df = {}
    for n in nodes:
        words = {w for w in re.split(r"[^a-z0-9]+", (n.get("description") or "").lower()) if w}
        v = VOCAB.get(n["id"]) or {}
        words |= set(v.get("keywords") or ())
        words |= set(v.get("categories") or ())
        for w in words:
            df[w] = df.get(w, 0) + 1
    _AFFINITY_CACHE["corpusdf"] = df
    return df


def dominated(nodes, cats):
    """Node ids whose own category holds a far more adopted sibling.

    This is what separates a query naming a crate from a query that merely uses
    a capability word which happens to be one. "config" leads Libraries/
    Configuration, so "read a config file" naming it means it; "websocket" is
    outdownloaded 110x by tungstenite in its own category, so "websocket server"
    is a capability phrase and crediting the name put a dead crate on top."""
    if "dominated" in _AFFINITY_CACHE:
        return _AFFINITY_CACHE["dominated"]
    by_id = {n["id"]: n for n in nodes}
    subtree = subtree_map(cats)
    out = set()
    for n in nodes:
        mine = popularity(n)[0] or 0
        for path in n.get("category_paths", []):
            members = subtree.get("cat:" + "/".join(path), ())
            for m in members:
                other = by_id.get(m)
                if other is None or m == n["id"]:
                    continue
                if (popularity(other)[0] or 0) >= mine * 10 and mine > 0:
                    out.add(n["id"])
                    break
            if n["id"] in out:
                break
    _AFFINITY_CACHE["dominated"] = out
    return out


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


def score_node(n, qt, cat_scores, name_df, anchored=None, corpus_df=None,
                dominated_ids=None):
    name = " ".join(n.get("names", [])).lower()
    name_parts = expand(p for p in re.split(r"[^a-z0-9]+", name) if p)
    # The RESOLVED crate, not the README-derived field. crate_name is populated
    # only where the list linked crates.io directly -- 536 of 1,633 -- so wiring
    # the exact-name bonus to it made the bonus unreachable for two thirds of the
    # index. Asking for "wgpu" returned Kiss3d, because wgpu could not claim it.
    crate = (crate_candidate(n)[0] or "").lower()
    corpus_df = corpus_df or {}
    desc = (n.get("description") or "").lower()
    desc_words = expand(re.split(r"[^a-z0-9]+", desc))

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

    # The name bonus fires only when the query IS the crate name, not merely
    # when it contains one. "websocket server" is a capability phrase that
    # happens to contain the crate `websocket`, and crediting it there put a
    # 2.5M-download crate above tungstenite at 282M inside the same category --
    # while the docs tell agents to query capabilities, which is exactly the
    # phrasing that triggered it.
    # The name counts unless this crate is an also-ran in its own category, in
    # which case the query is using a capability word that happens to be a name.
    name_counts = crate and n["id"] not in (dominated_ids or set())

    s = 0.0
    for t in qt:
        if t and t == crate and name_counts:
            # The query named this crate. Decisive when the name is an
            # identifier, near-worthless when it is a dictionary word: naming
            # "wgpu" can only mean one project, while "time" in "date and time"
            # means the concept, not the crate called time. Big enough to
            # outrank category inheritance, which is what buried wgpu when
            # Libraries/Graphics scored zero for its own crate's name.
            # Nothing at all when the name is a common word: "time" in
            # "date and time" names the concept, and crediting it at all
            # puts the crate `time` above chrono on a coincidence.
            s += 80 if corpus_df.get(t, 0) <= IDENT_DF_MAX else 0
        elif t in name_parts or stem(t) in name_parts:
            if t in covered:
                s += 1.0
            else:
                s += 14 if name_df.get(t, 0) <= 3 else 6
        elif t in name:
            s += 0.5          # substring only: weak, this is the "async-*" trap
        if t in desc_words or stem(t) in desc_words:
            s += 1.5
        elif t in desc:
            s += 0.5

    # The crate's own keywords and crates.io categories. Curated rather than
    # prose, so an exact hit is real evidence -- but kept below a category match,
    # since a keyword is one author's word for their crate, not a consensus.
    vocab = VOCAB.get(n["id"])
    if vocab:
        raw_own = set(vocab.get("keywords", ())) | set(vocab.get("categories", ()))
        own = expand(raw_own)
        kwdf = keyword_frequency()
        for t in qt:
            if t in own or stem(t) in own:
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
            scale = POP_WEIGHT if kind == "dl" else POP_WEIGHT * 2.2
            inside = (not anchored) or (best_path in anchored)
            if inside:
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
    years = staleness(n["id"])
    if years >= STALE_YEARS:
        when = (VOCAB.get(n["id"]) or {}).get("last_published", "")
        row = "%s  DORMANT: last published %s (%d years before this index)"
        out.append(row % (indent, when, years))
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
        return [], [], {}, {"confident": False, "coverage": 0.0, "named": False}

    profiles = category_terms(nodes, cats)
    cat_scores = {
        c["id"]: score_category(c, qt, profiles.get(c["id"])) for c in cats}
    name_df = name_frequency(nodes)
    cdf = corpus_frequency(nodes)
    dom = dominated(nodes, cats)
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
        ((score_node(n, qt, cat_scores, name_df, anchored, cdf, dom), n)
            for n in nodes),
        key=lambda x: (-x[0], x[1]["name"]))
    hits = [(sc, n) for sc, n in scored if sc > 0][:8]

    # Confidence is coverage, not score. A category can clear the floor while
    # explaining one token out of five -- "garbage collection tuning for the JVM
    # heap" anchors on Resources/Learning at 7.2 and returns Rustlings with no
    # hedge, while real queries explain nearly every token they are given. An
    # exact crate name is its own answer and needs no category to back it.
    covered = 0
    matched_tokens = set()
    if top_cats:
        path = " / ".join(top_cats[0]["path"]).lower()
        profile = profiles.get(top_cats[0]["id"]) or {}
        for t in qt:
            if t in path or any(term_match(t, k) for k in profile):
                covered += 1
                matched_tokens.add(t)
    # A name match only speaks for the whole query when it IS most of the query.
    # "quantum flux capacitor" hit the obscure crate `quantum` on one token of
    # three and called the whole thing confident.
    named = False
    if hits and len(qt) == 1:
        top_crate = (crate_candidate(hits[0][1])[0] or "").lower()
        exact = qt[0] == top_crate and cdf.get(qt[0], 0) <= IDENT_DF_MAX
        named = exact and hits[0][1]["id"] not in dom
    # Denominator is what the user typed, not what survived tokenising.
    # Everything non-ASCII is dropped before scoring, so a Japanese query in
    # which only "HTTP" survived was reporting 100% coverage -- the tool
    # understood a tenth of it and said so was all of it.
    # Measured in characters, not words: a Japanese query is one whitespace-
    # delimited word, so a word ratio still called it fully understood when only
    # the substring "HTTP" had survived.
    typed = len(re.sub(r"\s+", "", query))
    understood = sum(len(t) for t in qt if t in matched_tokens)
    coverage = min(understood / typed, 1.0) if typed else 0.0
    # Coverage alone still calls "frobnicate-o-matic" confident at 67%. Real
    # queries anchor at 16-54; incidental collisions land at 4-7. Both tests
    # must pass, unless the query simply named a crate.
    # Deliberately conservative. Weak-but-correct and weak-and-wrong overlap in
    # both score (6.9 vs 7.2) and coverage, so they cannot be separated cleanly.
    # Given that, err toward LOW: telling an agent to verify a correct answer
    # costs little, and presenting nonsense confidently is the failure that
    # matters. Queries reached through indirect vocabulary rather than a
    # category name genuinely are weaker evidence, and are marked as such.
    best_cat_score = cat_scores[top_cats[0]["id"]] if top_cats else 0.0
    confident = bool(named or (coverage >= 0.5 and best_cat_score >= 10.0))
    # A guessed crate name is not something to be confident about. The tool was
    # recommending "cargo add sha2" for password hashing at high confidence off
    # an UNVERIFIED guess; the marker existed but never reached the verdict.
    if hits and not crate_candidate(hits[0][1])[1]:
        confident = False
    # The tool already noticed this and printed it as an aside while still
    # stamping high: "command line arguments" led with a line-editing crate and
    # said so on its own line. A leading answer from a category the query never
    # matched is exactly the case to be unsure about.
    if hits and top_cats and not named:
        # Unless the query named the crate outright, in which case its category
        # is beside the point -- wgpu lives in Graphics, which never matches the
        # query "wgpu".
        lead_path = " / ".join(hits[0][1]["category_paths"][0])
        if lead_path not in {" / ".join(c["path"]) for c in top_cats}:
            confident = False
    conf = {"confident": confident, "coverage": coverage, "named": named}
    return hits, top_cats, subtree, conf


def cmd_ask(query):
    nodes, cats = load()
    if not tokens(query):
        # Not the same as empty. A query can be entirely non-ASCII, or a single
        # character, or punctuation, and saying "empty" tells an agent its input
        # was lost when in fact it was unparseable.
        if query.strip():
            print("UNPARSEABLE QUERY %r" % query)
            print("  Nothing in it survived tokenising. This index matches")
            print("  ASCII words of two or more characters.")
        else:
            print("EMPTY QUERY")
        return 1
    hits, top_cats, subtree, conf = rank(nodes, cats, query)
    node_by_id = {n["id"]: n for n in nodes}

    # One guidance block for every unhelpful outcome. Previously there were
    # three divergent paths and only the third was documented: a real technical
    # term the list lacks ("mmap") dead-ended with a bare "no match" and exit 1,
    # while pure punctuation got the full explanation and exit 0.
    def guidance():
        print("  The list has its own vocabulary and its own scope. Browse it")
        print("  and retry in its terms:")
        print("      python tools/rustgraph.py cat")
        print("      python tools/rustgraph.py recipe")

    if not hits:
        print("NO MATCH for %r" % query)
        print("  Nothing in the index scored against any word in this query.")
        guidance()
        return 1

    print("QUERY  %s" % query)
    print("=" * 72)
    # One explicit confidence line, always present and machine-readable. The old
    # signal was inverted: punctuation got a hedge and exit 0, a real term the
    # list lacks got a bare dead end and exit 1, and confident nonsense got
    # neither -- an agent could only tell them apart by scraping prose.
    label = "high" if conf["confident"] else "LOW"
    extra = ", exact crate name" if conf["named"] else ""
    banner = "CONFIDENCE  %s  (query coverage %.0f%%%s)"
    print(banner % (label, 100 * conf["coverage"], extra))
    if not conf["confident"]:
        print("  Verify before use. The index matched only part of this")
        print("  query, so the order below carries less evidence than usual.")
        guidance()

    if top_cats:
        print("\nMATCHED CATEGORIES")
        shown_paths = set()
        for c in top_cats:
            size = len(subtree.get(c["id"], []))
            print("  %-54s %d listed" % (" / ".join(c["path"]), size))
            shown_paths.add(" / ".join(c["path"]))
        # The leading answer is often filed under a category that never appears
        # in this list, which makes the documented next step -- copy a matched
        # category into `cat` to see alternatives -- browse the wrong list.
        lead_path = " / ".join(hits[0][1]["category_paths"][0])
        if lead_path not in shown_paths:
            aside = "  (top result is filed under %s, which did not match)"
            print(aside % lead_path)
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
        thin = False
        for score, count, other in partners[:5]:
            o = node_by_id.get(other)
            if not o:
                continue
            oc, _ov = crate_candidate(o)
            blurb = (o.get("description") or "")[:34]
            thin = thin or count < 8
            row = "    %-20s x%-5.1f in %2d  %s"
            print(row % (oc or o["name"], score, count, blurb))
        if thin:
            # The support count was printed but never characterised, so a pair
            # seen in five manifests read like a recommendation. For axum the
            # top partner is an SMTP library.
            print("    (co-occurrence across few manifests is weak evidence,")
            print("     not a recommendation -- check the count)")
        print()

    # Only verified names go in the runnable line. The per-result output marks
    # guesses with "?" and UNVERIFIED, but this line dropped that marking and
    # put the guess into something copy-pasteable -- "cargo add criterion-stats"
    # for a query about benchmarking, where the crate is criterion and
    # criterion-stats is a deprecated internal.
    picks, guessed, apps = [], [], []
    for _, n in hits[:6]:
        crate, verified = crate_candidate(n)
        if not crate:
            continue
        # An Applications entry is a program, not a dependency. The line was
        # offering "cargo add xargo probe-rs-tools" for things you run.
        if "Applications" in (n.get("sections") or []):
            if crate not in apps:
                apps.append(crate)
        elif verified and crate not in picks:
            picks.append(crate)
        elif not verified and crate not in guessed:
            guessed.append(crate)
    if picks:
        # One crate, not a concatenation. These results are mutual alternatives
        # by construction, since they share a category, so joining them gave
        # "cargo add reqwest hyper wreq async-graphql" -- four competing HTTP
        # crates in one manifest, one of them a GraphQL server.
        print("\nINSTALL  (pick one; the rest are alternatives)")
        print("  cargo add %s" % picks[0])
        if len(picks) > 1:
            print("  alternatives: %s" % ", ".join(picks[1:4]))
        # Selecting the crate is half the answer; the other half is which
        # feature to turn on, and getting that wrong fails at compile time
        # rather than here. Point at it, or the command goes undiscovered.
        lead_vocab = VOCAB.get(lead["id"]) or {}
        if lead_vocab.get("features") or lead_vocab.get("implicit_features"):
            note = "  %s has optional features -- check before building:"
            print(note % picks[0])
            print("      python tools/rustgraph.py features %s" % picks[0])
    if apps:
        print("\nPROGRAMS, NOT LIBRARIES: %s" % ", ".join(apps[:4]))
        print("  Listed under Applications. Install as tools, not dependencies.")
    if guessed:
        print("\nNOT INSTALLABLE AS-IS: %s" % ", ".join(guessed[:4]))
        print("  These names are guessed from the repository, not read from")
        print("  crates.io. Confirm each on crates.io before adding it.")
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
    # Accept the spaced form ask prints ("A / B / C") as well as the compact
    # one. Copying a category out of ask output is the documented recovery step,
    # and with only the compact form accepted it failed every time.
    want = re.sub(r"\s*/\s*", "/", path.strip()).strip("/").lower()
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
    """Everything known about one project, as parseable JSON.

    Three things were wrong here. Exact matching used crate_name, the README-only
    field, so `node tokio` could not match tokio by its crate at all and returned
    five unrelated substring hits with tokio itself cut off at position eight.
    The record was the raw nodes.jsonl line, missing the crate, downloads,
    dependencies and runtime that `ask` prints for the same project. And printing
    several objects in a loop emitted concatenated top-level JSON, which does not
    parse -- in the one command an agent would actually pipe to a parser."""
    if not (name or "").strip():
        # Was matching the empty string and dumping 69 results at exit 0, while
        # stack, features and example all print usage and exit 1.
        print("usage: node <name|crate>")
        return 1
    nodes, _cats = load()
    q = name.strip().lower()

    def crate_of(n):
        return (crate_candidate(n)[0] or "").lower()

    def named(n):
        return any(x.lower() == q for x in n.get("names", []))

    exact = [n for n in nodes if crate_of(n) == q or named(n)]
    hits = exact or [n for n in nodes
                        if q in " ".join(n.get("names", [])).lower()
                        or q in crate_of(n)]
    if not hits:
        print(json.dumps({"query": name, "matches": 0, "results": []}, indent=1))
        return 1

    req, opt = dep_split()
    node_by_id = {n["id"]: n for n in nodes}

    def crate_names(ids):
        out = []
        for i in sorted(ids):
            o = node_by_id.get(i)
            if o:
                out.append(crate_candidate(o)[0] or o["name"])
        return out

    results = []
    for n in hits[:10]:
        crate, verified = crate_candidate(n)
        pop, kind = popularity(n)
        vocab = VOCAB.get(n["id"]) or {}
        record = dict(n)
        record["crate"] = crate
        record["crate_verified"] = verified
        record["install"] = ("cargo add %s" % crate) if crate and verified else None
        record["downloads"] = pop if kind == "dl" else None
        record["runtime"] = runtime_note(n["id"]) or None
        record["keywords"] = vocab.get("keywords") or []
        record["requires"] = crate_names(req.get(n["id"], set()))
        record["optional_deps"] = crate_names(opt.get(n["id"], set()))
        record["readme_lines"] = n.get("lines")
        results.append(record)

    print(json.dumps({
        "query": name,
        "matches": len(hits),
        "returned": len(results),
        "truncated": len(hits) > len(results),
        "results": results,
    }, indent=1, sort_keys=True))
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
        hits, top_cats, _sub, _conf = rank(nodes, cats, case["q"])
        got = None
        if hits:
            got, _v = crate_candidate(hits[0][1])
        ok = got is not None and got.lower() == case["top"].lower()
        if not ok:
            failures.append("query %r: expected %s, got %s"
                            % (case["q"], case["top"], got or "no hit"))
        print("  %-4s %-32s -> %s" % ("ok" if ok else "FAIL", case["q"], got))

    for case in spec.get("no_anchor", []):
        _hits, top_cats, _sub, _conf = rank(nodes, cats, case["q"])
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

    for case in spec.get("confidence", []):
        _h, _t, _s, conf = rank(nodes, cats, case["q"])
        got = "high" if conf["confident"] else "low"
        ok = got == case["expect"]
        if not ok:
            failures.append("confidence %r: expected %s, got %s"
                            % (case["q"], case["expect"], got))
        mark = "ok" if ok else "FAIL"
        label = "confidence " + case["q"][:20]
        print("  %-4s %-32s -> %s" % (mark, label, got))

    for case in spec.get("features", []):
        want_crate = case["crate"].lower()
        node = next((n for n in nodes
                        if (crate_candidate(n)[0] or "").lower() == want_crate),
                    None)
        v = (VOCAB.get(node["id"]) if node else None) or {}
        pool = set(v.get("features") or {}) | set(v.get("implicit_features") or [])
        dflt = set(v.get("default_features") or [])
        ok = case["expect"] in pool and case["expect"] not in dflt
        if not ok:
            failures.append("features %s: %r not an opt-in feature"
                            % (case["crate"], case["expect"]))
        mark = "ok" if ok else "FAIL"
        label = "features %s/%s" % (case["crate"], case["expect"])
        print("  %-4s %-32s -> %s" % (mark, label[:32], "opt-in" if ok else "no"))

    for case in spec.get("examples", []):
        want_crate = case["crate"].lower()
        node = next((n for n in nodes
                        if (crate_candidate(n)[0] or "").lower() == want_crate),
                    None)
        v = (VOCAB.get(node["id"]) if node else None) or {}
        got = "present" if v.get("example") else "absent"
        ok = got == case["expect"]
        if not ok:
            failures.append("example %s: expected %s, got %s"
                            % (case["crate"], case["expect"], got))
        mark = "ok" if ok else "FAIL"
        print("  %-4s %-32s -> %s" % (mark, "example " + case["crate"], got))

    for case in spec.get("dormant", []):
        want_crate = case["crate"].lower()
        node = next((n for n in nodes
                        if (crate_candidate(n)[0] or "").lower() == want_crate),
                    None)
        years = staleness(node["id"]) if node else 0
        got = "dormant" if years >= STALE_YEARS else "current"
        ok = got == case["expect"]
        if not ok:
            failures.append("dormant %s: expected %s, got %s (%d years)"
                            % (case["crate"], case["expect"], got, years))
        mark = "ok" if ok else "FAIL"
        label = "dormant " + case["crate"]
        print("  %-4s %-32s -> %s" % (mark, label, got))

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
        # ask warns that a handful of manifests is weak evidence; this line was
        # emitting the same pairs as one runnable command with no caveat at all.
        print("\nINSTALL  (the crate, plus what is most often beside it)")
        print("  cargo add %s" % picks[0])
        if len(picks) > 1:
            print("  often added with: %s" % " ".join(picks[1:5]))
            print("  Co-occurrence, not requirements: the strongest partner for")
            print("  axum is an SMTP library seen in five manifests.")
    manifests = len(load_deps())
    note = "\nEvidence: co-occurrence across %d dependency manifests here."
    print(note % manifests)
    return 0


def cmd_features(args):
    """Which Cargo features a crate has, and which one you probably need.

    `ask` gets the crate right and then leaves the agent to fail at compile
    time: cargo add reqwest, call .json(), and it does not build, because that
    lives behind a feature. Both halves of the answer are in the dump.

    Optional dependencies matter as much as declared features. Cargo creates an
    implicit feature per optional dep, so chrono's `serde` feature appears in no
    features map anywhere -- it exists only because chrono optionally depends on
    serde, which this list does not even carry."""
    parts = (args or "").split()
    if not parts:
        print("usage: features <crate> [what you want]")
        return 1
    name, want = parts[0].lower(), " ".join(parts[1:]).lower()

    nodes, _cats = load()
    target = None
    for n in nodes:
        if (crate_candidate(n)[0] or "").lower() == name:
            target = n
            break
    if target is None:
        print("no indexed project publishes a crate called %r" % name)
        print("  Try: python tools/rustgraph.py node %s" % name)
        return 1

    crate, _v = crate_candidate(target)
    vocab = VOCAB.get(target["id"]) or {}
    declared = vocab.get("features") or {}
    implicit = vocab.get("implicit_features") or []
    default_on = set(vocab.get("default_features") or ())

    if not declared and not implicit:
        print("%s -- no feature data" % crate)
        print("  The crates.io dump carries no features map for this crate.")
        print("  Check docs.rs/%s for its feature list." % crate)
        return 1

    print("%s -- cargo features" % crate)
    print("=" * 72)

    def public(f):
        # Cargo internals, not knobs anyone should turn: "__tls" is private by
        # the leading-underscore convention, and a trailing "?" is weak-dep
        # syntax leaking out of the features map rather than a feature name.
        return f and not f.startswith("__") and not f.endswith("?")

    default_on = {f for f in default_on if public(f)}
    opt_in = sorted(f for f in declared
                    if public(f) and f != "default" and f not in default_on)
    implied = sorted(f for f in implicit
                        if public(f) and f not in declared
                        and f not in default_on)

    if want:
        matches = [f for f in opt_in + implied if want in f.lower()]
        for f in opt_in:
            if any(want in str(x).lower() for x in declared.get(f, ())):
                if f not in matches:
                    matches.append(f)
        print("\nFEATURES MATCHING %r" % want)
        if matches:
            for f in matches[:10]:
                enables = ", ".join(declared.get(f, ()))[:44]
                print("  %-22s %s" % (f, enables or "(optional dependency)"))
            print("\nCARGO.TOML")
            line = '  %s = { version = "*", features = ["%s"] }'
            print(line % (crate, matches[0]))
        else:
            print("  none. Listed below in full.")
        print()

    if default_on:
        print("ON BY DEFAULT  (set default-features = false to drop these)")
        print("  %s" % ", ".join(sorted(default_on)[:18]))
        print()
    if opt_in:
        print("OPT-IN  (add to features = [...])")
        for f in opt_in[:16]:
            enables = ", ".join(declared.get(f, ()))[:42]
            print("  %-22s %s" % (f, enables))
        if len(opt_in) > 16:
            print("  ... and %d more" % (len(opt_in) - 16))
        print()
    if implied:
        print("FROM OPTIONAL DEPENDENCIES  (implicit features)")
        print("  %s" % ", ".join(implied[:16]))
        if len(implied) > 16:
            print("  ... and %d more" % (len(implied) - 16))
    return 0


def cmd_example(name):
    """The crate's own starting example, lifted from its README.

    Coverage is 44% and that is stated rather than hidden: an absent example
    means this README carried none, not that none exists. clap is the notable
    gap -- its crates.io README defers to docs.rs."""
    if not name:
        print("usage: example <crate>")
        return 1
    nodes, _cats = load()
    q = name.strip().lower()
    target = next((n for n in nodes
                    if (crate_candidate(n)[0] or "").lower() == q), None)
    if target is None:
        print("no indexed project publishes a crate called %r" % name)
        return 1

    crate, _v = crate_candidate(target)
    vocab = VOCAB.get(target["id"]) or {}
    code = vocab.get("example")
    if not code:
        print("%s -- no example in its crates.io README" % crate)
        print("  That means this README carried none, not that none exists.")
        hint = "  Try docs.rs/%s, and `features %s` for what to enable."
        print(hint % (crate, crate))
        return 1

    print("%s -- starting example (from its crates.io README)" % crate)
    rt = runtime_note(target["id"])
    if rt:
        print("RUNTIME: %s" % rt)
    print("=" * 72)
    print(code)
    print("=" * 72)
    print("Verbatim from the README; it may assume features or a runtime.")
    print("  python tools/rustgraph.py features %s" % crate)
    return 0


def cmd_stats():
    nodes, cats = load()
    print("nodes          %d" % len(nodes))
    # Verified only. crate_name is README-only and undercounts at 536;
    # crate_candidate falls back to the repository name and overcounts at 1633,
    # since 369 of those are guesses with no resolved identity. The documented
    # 1,264 is the number of crates actually resolved against crates.io.
    resolved = sum(1 for n in nodes if IDENT.get(n["id"]))
    guessed = sum(1 for n in nodes
                    if not IDENT.get(n["id"]) and crate_candidate(n)[0])
    print("  with crate   %d resolved" % resolved)
    print("  guessed name %d (repo-derived, unverified)" % guessed)
    print("  multi-listed %d" % sum(1 for n in nodes if n.get("listings", 1) > 1))
    # Always zero on a fresh clone, since the enriched artifact is gitignored;
    # a permanent "enriched 0" reads as a defect rather than an absent input.
    enriched = sum(1 for n in nodes if n.get("metrics"))
    if enriched:
        print("  enriched     %d" % enriched)
    print("categories     %d" % len(cats))
    print("\nlargest categories:")
    for c in sorted(cats, key=lambda c: -len(c["members"]))[:12]:
        print("  %-52s %3d" % ("/".join(c["path"]), len(c["members"])))
    return 0


def main(argv):
    # A legacy Windows console encodes stdout as cp1252, which cannot represent
    # a query the user actually typed: echoing a Japanese query crashed with
    # UnicodeEncodeError and exit 120. Degrade the echo rather than the run.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
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
        # A typo of --with-dev used to be ignored, handing back the wrong graph
        # with no warning.
        unknown = [a for a in rest if a not in ("--write", "--with-dev")]
        if unknown:
            print("unknown option(s): %s" % " ".join(unknown))
            print("usage: dag [--write] [--with-dev]")
            return 1
        return cmd_dag("--write" in rest, "--with-dev" in rest)
    if cmd == "recipe":
        return cmd_recipe(" ".join(rest))
    if cmd == "example":
        return cmd_example(" ".join(rest))
    if cmd == "features":
        return cmd_features(" ".join(rest))
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
