#!/usr/bin/env python3
"""crates_deps.py -- resolve crate identity and dependency edges from the
crates.io database dump.

Supplies the three things README.md cannot: crate identity for entries with no
crates.io link, reverse-mapped from the GitHub URL via the dump's `repository`
column; download counts, so ranking is no longer popularity-blind; and
depends_on edges between indexed projects, which is the actual DAG.

The dump is ~1.8GB compressed and ~20GB unpacked, so it is streamed twice
straight out of the tarball and filtered to our nodes; nothing is unpacked.

    python tools/crates_deps.py <path/to/db-dump.tar.gz>

Writes index/crate-identity.json, index/downloads.json, index/dep-edges.jsonl
and index/crate-vocabulary.json.
"""

import csv
import io
import json
import re
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NODES = ROOT / "index" / "nodes.jsonl"
IDENTITY = ROOT / "index" / "crate-identity.json"
DOWNLOADS = ROOT / "index" / "downloads.json"
DEP_EDGES = ROOT / "index" / "dep-edges.jsonl"
KEYWORDS = ROOT / "index" / "crate-vocabulary.json"

csv.field_size_limit(1 << 30)

GH = re.compile(r"github\.com[/:]+([^/]+)/([^/#?]+)", re.I)


def norm_repo(url):
    """https://github.com/Owner/Repo.git -> https://github.com/owner/repo"""
    if not url:
        return None
    m = GH.search(url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    repo = repo[:-4] if repo.lower().endswith(".git") else repo
    return "https://github.com/%s/%s" % (owner.lower(), repo.lower())


def text_lines(fileobj):
    """Decode a stream-mode tar member into lines without TextIOWrapper, which
    needs a seekable file the "r|gz" reader does not provide. csv.reader takes
    any line iterator and still handles newlines inside quoted fields."""
    dec = io.IncrementalNewlineDecoder(None, translate=True)
    utf8 = __import__("codecs").getincrementaldecoder("utf-8")()
    buf = ""
    while True:
        chunk = fileobj.read(1 << 20)
        if not chunk:
            buf += dec.decode(utf8.decode(b"", True), True)
            if buf:
                yield buf
            return
        buf += dec.decode(utf8.decode(chunk))
        parts = buf.split("\n")
        buf = parts.pop()
        for line in parts:
            yield line + "\n"


SNAPSHOT = {"id": None}


def members(tf_path, wanted):
    """Stream the tarball, yielding (basename, line iterator) for wanted files."""
    with tarfile.open(tf_path, "r|gz") as tf:
        for m in tf:
            if SNAPSHOT["id"] is None and "/" in m.name:
                # Dumps unpack into a dated directory; keep it so a consumer can
                # see how stale these artifacts are.
                SNAPSHOT["id"] = m.name.split("/")[0]
            base = m.name.rsplit("/", 1)[-1]
            if base in wanted and "/data/" in m.name:
                f = tf.extractfile(m)
                if f is not None:
                    yield base, text_lines(f)


def pg_array(raw):
    """Postgres array literal -> list. '{db,database,memory-map}' -> [...]."""
    s = (raw or "").strip()
    if not s or s == "{}":
        return []
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1]
    out = []
    for part in s.split(","):
        part = part.strip().strip('"').strip()
        if part:
            out.append(part.lower())
    return out


def feature_map(raw):
    """The crate's declared features and what each one turns on.

    Bounded: a handful of crates declare thousands of features and nothing is
    served by carrying them all. Median is four."""
    try:
        m = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return {}
    if not isinstance(m, dict):
        return {}
    out = {}
    for name in sorted(m)[:80]:
        vals = m.get(name)
        out[name] = [str(v) for v in vals][:12] if isinstance(vals, list) else []
    return out


def default_closure(raw):
    """Feature names reachable from `default`.

    Cargo's "optional" does not mean "off": axum gates tokio behind a feature
    that `default` turns on, so a plain optional flag would report axum as not
    needing a runtime. Walking the feature graph from `default` separates
    genuinely opt-in dependencies from ones you get unless you opt out."""
    try:
        features = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return set()
    if not isinstance(features, dict):
        return set()
    seen, stack = set(), list(features.get("default") or [])
    while stack:
        name = str(stack.pop())
        name = name.split("/", 1)[0]
        if name.startswith("dep:"):
            name = name[4:]
        if not name or name in seen:
            continue
        seen.add(name)
        stack.extend(features.get(name) or [])
    return seen


FENCE = re.compile(r"```([^\r\n]*)\r?\n(.*?)```", re.S)


def first_example(readme, max_lines=28):
    """The first runnable-looking Rust block from a README.

    The infostring has to be parsed tolerantly: fences are tagged rust, rs,
    rust,no_run, rust,ignore or nothing at all, and a pattern that stops at the
    comma silently loses 205 blocks including reqwest's only one. A block counts
    as code if it declares or uses something; prose fences do not."""
    if not readme:
        return None
    for lang, body in FENCE.findall(readme):
        tag = re.split(r"[,\s]+", lang.strip().lower())[0] if lang.strip() else ""
        if tag not in ("rust", "rs", "ignore", "no_run", "should_panic", ""):
            continue
        if not any(k in body for k in ("fn ", "use ", "let ")):
            continue
        lines = [l.rstrip() for l in body.strip().splitlines()]
        if len(lines) < 2:
            continue
        if len(lines) > max_lines:
            lines = lines[:max_lines] + ["// ... truncated"]
        return chr(10).join(lines)
    return None


def repo_aliases(repo_name):
    """Names a repo might publish under. 'curl-rust' -> {'curl-rust','curl'},
    so the user-facing crate wins over a sys- crate that out-downloads it only
    because the user-facing one depends on it."""
    n = (repo_name or "").lower()
    out = {n}
    for suf in ("-rs", "-rust", ".rs", "_rs", "_rust"):
        if n.endswith(suf) and len(n) > len(suf):
            out.add(n[: -len(suf)])
    for pre in ("rust-", "rust_"):
        if n.startswith(pre) and len(n) > len(pre):
            out.add(n[len(pre):])
    for x in list(out):
        out.add(x.replace("-", "_"))
        out.add(x.replace("_", "-"))
    return {x for x in out if x}


def load_nodes():
    if not NODES.exists():
        sys.exit("index/nodes.jsonl missing -- run: cargo run --bin index")
    return [json.loads(l) for l in NODES.read_text(encoding="utf-8").splitlines() if l.strip()]


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    dump = argv[1]
    nodes = load_nodes()

    target_names = {n["crate_name"].lower() for n in nodes if n.get("crate_name")}
    repo_to_nodes = {}
    for n in nodes:
        r = norm_repo(n.get("github_url") or n.get("primary_url") or "")
        if r:
            repo_to_nodes.setdefault(r, []).append(n)
    print("nodes %d | readme crate names %d | github repos %d"
            % (len(nodes), len(target_names), len(repo_to_nodes)))

    # ---- pass 1: crates.csv (identity + downloads) then versions.csv (latest)
    crates = {}          # crate_id -> {name, downloads, repo}
    all_names = {}       # crate_id -> name, for every crate in the dump
    by_name = {}         # name -> crate_id
    by_repo = {}         # normalised repo -> [crate_id]
    latest = {}          # crate_id -> (created_at, version_id)

    print("pass 1: crates.csv + versions.csv ...")
    for base, stream in members(dump, {"crates.csv", "versions.csv"}):
        r = csv.DictReader(stream)
        if base == "crates.csv":
            for row in r:
                name = (row.get("name") or "").strip()
                # Every crate's name, not just the kept ones. Cargo creates an
                # implicit feature per optional dependency, and those deps are
                # mostly outside this list -- chrono's `serde` feature exists
                # only because it optionally depends on serde, which the list
                # does not carry.
                all_names[row["id"]] = name
                repo = norm_repo(row.get("repository") or "")
                keep = name.lower() in target_names or (repo and repo in repo_to_nodes)
                if not keep:
                    continue
                cid = row["id"]
                # Current dumps carry no crate-level downloads column; the
                # total is the sum over versions, accumulated in pass 1b below.
                crates[cid] = {
                    "name": name,
                    "downloads": 0,
                    "repo": repo,
                    # Kept only long enough to lift the first code example out;
                    # the raw text is never written anywhere.
                    "readme": (row.get("readme") or "").strip(),
                }
                by_name.setdefault(name.lower(), cid)
                if repo:
                    by_repo.setdefault(repo, []).append(cid)
            print("  kept %d crates" % len(crates))
        else:
            for row in r:
                cid = row.get("crate_id")
                if cid not in crates:
                    continue
                # Yanked versions still count toward adoption, but must never
                # be chosen as the version whose dependencies we read.
                try:
                    crates[cid]["downloads"] += int(row.get("downloads") or 0)
                except ValueError:
                    pass
                if (row.get("yanked") or "").lower() in ("t", "true"):
                    continue
                created = row.get("created_at") or ""
                cur = latest.get(cid)
                if cur is None or created > cur[0]:
                    # Keywords are author-assigned synonyms and categories are
                    # crates.io's own taxonomy -- a second opinion on what this
                    # crate is, in vocabulary the README never uses.
                    latest[cid] = (
                        created, row["id"],
                        pg_array(row.get("keywords")),
                        pg_array(row.get("categories")),
                        sorted(default_closure(row.get("features"))),
                        feature_map(row.get("features")))
            print("  latest versions for %d crates" % len(latest))

    # ---- resolve each node to a crate
    ver_to_crate = {v[1]: cid for cid, v in latest.items()}
    identity = {}
    for n in nodes:
        cid, source = None, None
        cn = (n.get("crate_name") or "").lower()
        if cn and cn in by_name:
            cid, source = by_name[cn], "readme"
        else:
            r = norm_repo(n.get("github_url") or n.get("primary_url") or "")
            cands = by_repo.get(r, []) if r else []
            if cands:
                repo_name = (n.get("repo") or {}).get("name", "").lower()
                # One repo usually ships many crates (tokio-rs/tokio publishes
                # tokio, tokio-util, tokio-stream, ...), so resolve by
                # confidence: exact repo name, then a conventional alias of it,
                # and only then the most downloaded crate in the repo.
                exact = [c for c in cands if crates[c]["name"].lower() == repo_name]
                aliases = repo_aliases(repo_name)
                alias = [c for c in cands if crates[c]["name"].lower() in aliases]
                if exact:
                    cid, source = exact[0], "repo-exact"
                elif alias:
                    cid = max(alias, key=lambda c: crates[c]["downloads"])
                    source = "repo-alias"
                else:
                    cid = max(cands, key=lambda c: crates[c]["downloads"])
                    source = "repo-downloads"
        if cid:
            identity[n["id"]] = {
                "crate": crates[cid]["name"],
                "crate_id": cid,
                "source": source,
                "downloads": crates[cid]["downloads"],
                "candidates": sorted({crates[c]["name"] for c in by_repo.get(
                    crates[cid]["repo"], [])}) if crates[cid]["repo"] else [],
            }

    node_of_crate = {v["crate_id"]: k for k, v in identity.items()}
    want_versions = {v[1] for cid, v in latest.items() if cid in node_of_crate}

    # ---- pass 2: dependencies.csv, filtered to edges between indexed projects
    print("pass 2: dependencies.csv ...")
    edges, seen = [], set()
    implicit = {}        # crate_id -> optional dep names (implicit features)
    for base, stream in members(dump, {"dependencies.csv"}):
        r = csv.DictReader(stream)
        for row in r:
            vid = row.get("version_id")
            if vid not in want_versions:
                continue
            dep_cid = row.get("crate_id")
            if ((row.get("optional") or "").lower() in ("t", "true")
                    and row.get("kind") == "0"):
                owner = ver_to_crate.get(vid)
                if owner:
                    implicit.setdefault(owner, set()).add(
                        all_names.get(dep_cid, ""))
            if dep_cid not in node_of_crate:
                continue
            src_cid = ver_to_crate.get(vid)
            if not src_cid or src_cid == dep_cid:
                continue
            a, b = node_of_crate.get(src_cid), node_of_crate[dep_cid]
            if not a or (a, b) in seen:
                continue
            seen.add((a, b))
            edges.append({
                "from": a, "to": b, "kind": "depends_on",
                "dep_kind": row.get("kind") or "0",
                "optional": (row.get("optional") or "").lower() in ("t", "true"),
            })

    by_source = {}
    for v in identity.values():
        by_source[v["source"]] = by_source.get(v["source"], 0) + 1

    # Two nodes resolving to one crate means either the list entered the same
    # project twice under different URLs -- which node collapse cannot see,
    # since it keys on the URL -- or we misresolved one of them. Either way it
    # should be visible rather than silently averaged away.
    seen_crate = {}
    collisions = {}
    for node_id, v in identity.items():
        first = seen_crate.setdefault(v["crate"], node_id)
        if first != node_id:
            collisions.setdefault(v["crate"], [first]).append(node_id)

    vocab = {}
    for node_id, v in identity.items():
        entry = latest.get(v["crate_id"])
        if not entry:
            continue
        keywords, categories, default_on = entry[2], entry[3], entry[4]
        declared = entry[5] if len(entry) > 5 else {}
        implied = sorted(x for x in implicit.get(v["crate_id"], set()) if x)
        if keywords or categories or default_on or declared or implied:
            vocab[node_id] = {
                "keywords": keywords,
                "last_published": (entry[0] or "")[:10] if entry else "",
                "categories": categories,
                "default_features": default_on,
                "features": declared,
                "implicit_features": implied,
                "example": first_example(
                    (crates.get(v["crate_id"]) or {}).get("readme")),
            }
    KEYWORDS.write_text(
        json.dumps(vocab, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    IDENTITY.write_text(
        json.dumps(identity, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    downloads = {k: v["downloads"] for k, v in identity.items()}
    DOWNLOADS.write_text(
        json.dumps(downloads, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    DEP_EDGES.write_text(
        "".join(json.dumps(e, sort_keys=True) + "\n"
                for e in sorted(edges, key=lambda e: (e["from"], e["to"]))),
        encoding="utf-8")

    (ROOT / "index" / "crates-provenance.json").write_text(json.dumps({
        "dump_snapshot": SNAPSHOT["id"],
        "source": "https://static.crates.io/db-dump.tar.gz",
        "nodes": len(nodes),
        "identity_resolved": len(identity),
        "identity_by_source": by_source,
        "dep_edges": len(edges),
        "crate_collisions": {k: sorted(v) for k, v in collisions.items()},
    }, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    print("\ndump snapshot %s" % SNAPSHOT["id"])
    print("identity resolved for %d/%d nodes" % (len(identity), len(nodes)))
    for k in sorted(by_source):
        print("  %-16s %d" % (k, by_source[k]))
    print("depends_on edges between indexed projects: %d" % len(edges))
    if collisions:
        print("\ncrate collisions (one crate, several list entries):")
        for crate, ids in sorted(collisions.items()):
            print("  %s" % crate)
            for i in ids:
                print("      %s" % i)
    print("vocabulary: %d crates carry keywords or crates.io categories"
        % len(vocab))
    print("wrote %s, %s, %s, %s" % (IDENTITY.name, DOWNLOADS.name,
                                    DEP_EDGES.name, KEYWORDS.name))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
