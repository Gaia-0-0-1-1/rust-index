//! Agent index, phases 0 and 1.
//!
//! P0 `extract`: README.md -> index/entries.jsonl. One node per curated entry,
//!               carrying its category path, its identity URLs, and a
//!               README.md:line citation so an agent can act without scanning.
//! P1 `enrich`:  join those nodes against results/popularity.yaml -- the cache
//!               the link checker (src/main.rs) already fills -- so indexing
//!               issues no API traffic of its own.
//!
//! README.md stays the single source of truth. Everything under index/ is
//! generated: re-run this, never hand-edit it.
//!
//!   cargo run --bin index -- extract
//!   cargo run --bin index -- enrich
//!   cargo run --bin index              # both

use anyhow::{Context, Result};
use lazy_static::lazy_static;
use pulldown_cmark::{Event, Parser, Tag};
use regex::Regex;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;

lazy_static! {
    // Mirrored verbatim from src/main.rs so our join keys are byte-identical to
    // the ones the checker writes into results/popularity.yaml.
    static ref GITHUB_REPO_REGEX: Regex =
        Regex::new(r"^https://github.com/(?P<org>[^/]+)/(?P<repo>[^/^#]+)(.*)").unwrap();
    static ref CRATE_REGEX: Regex =
        Regex::new(r"https://crates.io/crates/(?P<crate>[^/]+)/?$").unwrap();
}

const README: &str = "README.md";
const INDEX_DIR: &str = "index";
const ENTRIES: &str = "index/entries.jsonl";
const ENRICHED: &str = "index/entries.enriched.jsonl";
const COVERAGE: &str = "index/coverage.json";
const NODES: &str = "index/nodes.jsonl";
const EDGES: &str = "index/edges.jsonl";
const CATEGORIES: &str = "index/categories.json";
const POPULARITY: &str = "results/popularity.yaml";

#[derive(Debug, Clone, Serialize, Deserialize)]
struct LinkRef {
    url: String,
    text: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Repo {
    host: String,
    owner: String,
    name: String,
}

#[derive(Debug, Clone, Copy, Default, Serialize, Deserialize)]
struct Metrics {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    github_stars: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    github_rust_percentage: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    cargo_downloads: Option<u32>,
}

impl Metrics {
    fn is_empty(&self) -> bool {
        self.github_stars.is_none()
            && self.github_rust_percentage.is_none()
            && self.cargo_downloads.is_none()
    }
}

/// One curated entry: a node in the graph P2 will build edges over.
#[derive(Debug, Clone, Serialize, Deserialize)]
struct Entry {
    /// Stable node id. The primary URL, which is also the popularity join key.
    id: String,
    name: String,
    /// Top-level "## " heading: Applications / Development tools / Libraries / ...
    section: String,
    /// Heading trail plus the bullet-group label, e.g.
    /// ["Libraries", "Command-line", "Argument parsing"].
    category_path: Vec<String>,
    primary_url: String,
    /// Normalised https://github.com/{org}/{repo} -- the github_stars key.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    github_url: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    crate_name: Option<String>,
    /// Raw crates.io URL exactly as written -- the cargo_downloads key.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    crates_io_url: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    repo: Option<Repo>,
    description: String,
    /// Non-primary, non-crate links inside the entry (specs, docs, related work).
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    mentions: Vec<LinkRef>,
    badges: usize,
    /// README.md line, so downstream tools can cite instead of re-scanning.
    line: usize,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    metrics: Option<Metrics>,
}

#[derive(Default)]
struct PendingLink {
    url: String,
    text: String,
    has_image: bool,
}

#[derive(Default)]
struct ItemState {
    text: String,
    links: Vec<LinkRef>,
    badges: usize,
    offset: usize,
    /// Set when this item contains a nested list.
    has_child_list: bool,
    /// Set when this item is a pure grouping label: it has a nested list and no
    /// " - " description, so it names a technology rather than a Rust project.
    is_label: bool,
}

fn line_starts(s: &str) -> Vec<usize> {
    let mut v = vec![0usize];
    for (i, b) in s.bytes().enumerate() {
        if b == b'\n' {
            v.push(i + 1);
        }
    }
    v
}

fn line_of(starts: &[usize], offset: usize) -> usize {
    match starts.binary_search(&offset) {
        Ok(i) => i + 1,
        Err(i) => i,
    }
}

fn normalise_github(url: &str) -> Option<String> {
    if GITHUB_REPO_REGEX.is_match(url) {
        Some(
            GITHUB_REPO_REGEX
                .replace_all(url, "https://github.com/$org/$repo")
                .to_string(),
        )
    } else {
        None
    }
}

fn build_entry(
    item: &ItemState,
    headings: &[(u32, String)],
    groups: &[Option<String>],
    starts: &[usize],
) -> Option<Entry> {
    // Items with no outbound link are bullet-group labels ("* Argument parsing")
    // or table-of-contents anchors -- structure, not nodes.
    let primary = item.links.iter().find(|l| !l.url.starts_with('#'))?;

    let text = item.text.trim();
    let description = match text.find(" - ") {
        Some(i) => text[i + 3..].trim().to_string(),
        None => String::new(),
    };

    let mut category_path: Vec<String> = headings
        .iter()
        .filter(|(level, _)| *level >= 2)
        .map(|(_, t)| t.clone())
        .collect();
    let section = category_path.first().cloned().unwrap_or_default();
    category_path.extend(groups.iter().flatten().cloned());

    let crate_link = item.links.iter().find(|l| CRATE_REGEX.is_match(&l.url));
    let crate_name = crate_link.and_then(|l| {
        CRATE_REGEX
            .captures(&l.url)
            .and_then(|c| c.name("crate").map(|m| m.as_str().to_string()))
    });

    let github_url = item.links.iter().find_map(|l| normalise_github(&l.url));
    let repo = github_url.as_ref().and_then(|u| {
        GITHUB_REPO_REGEX.captures(u).map(|c| Repo {
            host: "github".to_string(),
            owner: c.name("org").map(|m| m.as_str()).unwrap_or("").to_string(),
            name: c.name("repo").map(|m| m.as_str()).unwrap_or("").to_string(),
        })
    });

    let crate_url = crate_link.map(|l| l.url.clone());
    let mentions: Vec<LinkRef> = item
        .links
        .iter()
        .filter(|l| {
            !l.url.starts_with('#') && l.url != primary.url && Some(&l.url) != crate_url.as_ref()
        })
        .cloned()
        .collect();

    Some(Entry {
        id: primary.url.clone(),
        name: primary.text.trim().to_string(),
        section,
        category_path,
        primary_url: primary.url.clone(),
        github_url,
        crate_name,
        crates_io_url: crate_url,
        repo,
        description,
        mentions,
        badges: item.badges,
        line: line_of(starts, item.offset),
        metrics: None,
    })
}

fn extract() -> Result<Vec<Entry>> {
    fs::create_dir_all(INDEX_DIR).with_context(|| format!("creating {}", INDEX_DIR))?;
    let md = fs::read_to_string(README).with_context(|| format!("reading {}", README))?;
    let starts = line_starts(&md);

    let mut headings: Vec<(u32, String)> = Vec::new();
    let mut in_heading: Option<u32> = None;
    let mut heading_text = String::new();

    let mut items: Vec<ItemState> = Vec::new();
    let mut groups: Vec<Option<String>> = Vec::new();
    let mut pending: Vec<PendingLink> = Vec::new();
    let mut image_depth: usize = 0;

    let mut entries: Vec<Entry> = Vec::new();
    let mut labels: usize = 0;
    let mut in_toc = false;

    for (event, range) in Parser::new(&md).into_offset_iter() {
        // The mktoc block is generated navigation, not content. Skipping both
        // its starts and its ends keeps the item/list stacks balanced.
        if let Event::Html(ref content) = event {
            if content.contains("BEGIN mktoc") {
                in_toc = true;
            } else if content.contains("END mktoc") {
                in_toc = false;
            }
            continue;
        }
        if in_toc {
            continue;
        }
        match event {
            Event::Start(Tag::Heading(level)) => {
                in_heading = Some(level);
                heading_text.clear();
            }
            Event::End(Tag::Heading(level)) => {
                headings.retain(|(l, _)| *l < level);
                headings.push((level, heading_text.trim().to_string()));
                in_heading = None;
            }
            Event::Start(Tag::List(_)) => {
                // Any item holding a nested list contributes a category segment.
                // Whether it is ALSO an entry depends on its description:
                //   "* [Eclipse](https://www.eclipse.org/)"        -> label only
                //   "* AWS [[aws](https://crates.io/keywords/aws)]" -> label only
                //   "* [Cargo](https://crates.io/) - the package manager"
                //                                    -> label AND a real entry
                let label = match items.last_mut() {
                    Some(it) => {
                        it.has_child_list = true;
                        let raw = it.text.trim();
                        it.is_label = !raw.contains(" - ");
                        // Keep the name: drop the description and any trailing
                        // "[crate]" / "[keyword]" annotation.
                        let name = raw
                            .split(" - ")
                            .next()
                            .unwrap_or(raw)
                            .split(" [")
                            .next()
                            .unwrap_or(raw)
                            .trim();
                        if name.is_empty() {
                            None
                        } else {
                            Some(name.to_string())
                        }
                    }
                    None => None,
                };
                groups.push(label);
            }
            Event::End(Tag::List(_)) => {
                groups.pop();
            }
            Event::Start(Tag::Item) => {
                items.push(ItemState {
                    offset: range.start,
                    ..Default::default()
                });
            }
            Event::End(Tag::Item) => {
                if let Some(it) = items.pop() {
                    if it.is_label {
                        labels += 1;
                        continue;
                    }
                    if let Some(entry) = build_entry(&it, &headings, &groups, &starts) {
                        entries.push(entry);
                    } else {
                        // Linkless, childless bullet: structural, not a node.
                        labels += 1;
                    }
                }
            }
            Event::Start(Tag::Link(_, url, _)) => {
                pending.push(PendingLink {
                    url: url.to_string(),
                    ..Default::default()
                });
            }
            Event::End(Tag::Link(..)) => {
                if let Some(p) = pending.pop() {
                    if in_heading.is_some() {
                        heading_text.push_str(&p.text);
                    } else if let Some(it) = items.last_mut() {
                        // A link wrapping an image is a CI badge, not a citation.
                        if p.has_image {
                            it.badges += 1;
                        } else {
                            it.text.push_str(&p.text);
                            it.links.push(LinkRef {
                                url: p.url,
                                text: p.text,
                            });
                        }
                    }
                }
            }
            Event::Start(Tag::Image(..)) => {
                if let Some(p) = pending.last_mut() {
                    p.has_image = true;
                }
                image_depth += 1;
            }
            Event::End(Tag::Image(..)) => {
                image_depth = image_depth.saturating_sub(1);
            }
            Event::Text(t) | Event::Code(t) => {
                if image_depth > 0 {
                    continue;
                }
                if in_heading.is_some() {
                    heading_text.push_str(&t);
                } else if let Some(p) = pending.last_mut() {
                    p.text.push_str(&t);
                } else if let Some(it) = items.last_mut() {
                    it.text.push_str(&t);
                }
            }
            Event::SoftBreak | Event::HardBreak => {
                if image_depth > 0 {
                    continue;
                }
                if in_heading.is_some() {
                    heading_text.push(' ');
                } else if let Some(it) = items.last_mut() {
                    it.text.push(' ');
                }
            }
            _ => {}
        }
    }

    // The gate, enforced rather than merely reported: every "* " bullet outside
    // the generated TOC must be accounted for as exactly one entry or exactly
    // one label. Drift here means parsing changed and the index is untrustworthy,
    // so fail loudly instead of writing a plausible-looking wrong file.
    let mut bullets = 0usize;
    let mut scanning_toc = false;
    let mut in_fence = false;
    for line in md.lines() {
        let t = line.trim_start();
        // A "* " inside a fenced block is sample text, not a list item. The
        // README has no fences today, but counting one would fail this gate for
        // a reason that has nothing to do with parsing drift.
        if t.starts_with("```") || t.starts_with("~~~") {
            in_fence = !in_fence;
        } else if in_fence {
            continue;
        } else if line.contains("BEGIN mktoc") {
            scanning_toc = true;
        } else if line.contains("END mktoc") {
            scanning_toc = false;
        } else if !scanning_toc && t.starts_with("* ") {
            bullets += 1;
        }
    }
    if entries.len() + labels != bullets {
        anyhow::bail!(
            "gate failed: {} entries + {} labels = {}, but README.md has {} bullets \
                outside the TOC. Parsing has drifted; do not trust the index.",
            entries.len(),
            labels,
            entries.len() + labels,
            bullets
        );
    }

    let mut out = String::new();
    for e in &entries {
        out.push_str(&serde_json::to_string(e)?);
        out.push('\n');
    }
    fs::write(ENTRIES, out).with_context(|| format!("writing {}", ENTRIES))?;

    let categories: BTreeSet<String> = entries
        .iter()
        .map(|e| e.category_path.join(" / "))
        .collect();
    println!("P0 extract -> {}", ENTRIES);
    println!("  entries       {}", entries.len());
    println!("  labels        {}", labels);
    println!(
        "  gate OK       {} == {} bullets outside the TOC",
        entries.len() + labels,
        bullets
    );
    println!(
        "  with crate    {}",
        entries.iter().filter(|e| e.crate_name.is_some()).count()
    );
    println!(
        "  with github   {}",
        entries.iter().filter(|e| e.github_url.is_some()).count()
    );
    println!("  categories    {}", categories.len());
    Ok(entries)
}

#[derive(Debug, Default, Deserialize)]
struct PopularityData {
    #[serde(default)]
    github_stars: BTreeMap<String, u32>,
    #[serde(default)]
    github_rust_percentage: BTreeMap<String, f64>,
    #[serde(default)]
    cargo_downloads: BTreeMap<String, u32>,
}

#[derive(Debug, Default, Serialize)]
struct SectionCoverage {
    entries: usize,
    with_crate: usize,
    enriched: usize,
}

#[derive(Debug, Serialize)]
struct Coverage {
    entries: usize,
    /// Rows are occurrence-level: a project listed under several categories
    /// appears once per listing. P2 must collapse on primary_url before
    /// building edges, or one project becomes several nodes.
    unique_ids: usize,
    multi_listed: usize,
    with_github: usize,
    with_crate: usize,
    popularity_cache_present: bool,
    enriched: usize,
    with_stars: usize,
    with_rust_percentage: usize,
    with_downloads: usize,
    by_section: BTreeMap<String, SectionCoverage>,
}

fn enrich() -> Result<()> {
    let raw = fs::read_to_string(ENTRIES)
        .with_context(|| format!("reading {} (run `extract` first)", ENTRIES))?;
    let mut entries: Vec<Entry> = raw
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(serde_json::from_str)
        .collect::<Result<_, _>>()
        .context("parsing entries.jsonl")?;

    let cache_raw = fs::read_to_string(POPULARITY).ok();
    let present = cache_raw.is_some();
    let pop: PopularityData = match &cache_raw {
        Some(s) => serde_yaml::from_str(s).context("parsing popularity.yaml")?,
        None => PopularityData::default(),
    };

    for e in entries.iter_mut() {
        let mut m = Metrics::default();
        if let Some(g) = &e.github_url {
            m.github_stars = pop.github_stars.get(g).copied();
            m.github_rust_percentage = pop.github_rust_percentage.get(g).copied();
        }
        if let Some(c) = &e.crates_io_url {
            m.cargo_downloads = pop.cargo_downloads.get(c).copied();
        }
        e.metrics = if m.is_empty() { None } else { Some(m) };
    }

    let mut out = String::new();
    for e in &entries {
        out.push_str(&serde_json::to_string(e)?);
        out.push('\n');
    }
    fs::write(ENRICHED, out).with_context(|| format!("writing {}", ENRICHED))?;

    let mut by_section: BTreeMap<String, SectionCoverage> = BTreeMap::new();
    for e in &entries {
        let s = by_section.entry(e.section.clone()).or_default();
        s.entries += 1;
        if e.crate_name.is_some() {
            s.with_crate += 1;
        }
        if e.metrics.is_some() {
            s.enriched += 1;
        }
    }

    let mut id_counts: BTreeMap<&str, usize> = BTreeMap::new();
    for e in &entries {
        *id_counts.entry(e.id.as_str()).or_default() += 1;
    }

    let cov = Coverage {
        entries: entries.len(),
        unique_ids: id_counts.len(),
        multi_listed: id_counts.values().filter(|c| **c > 1).count(),
        with_github: entries.iter().filter(|e| e.github_url.is_some()).count(),
        with_crate: entries.iter().filter(|e| e.crate_name.is_some()).count(),
        popularity_cache_present: present,
        enriched: entries.iter().filter(|e| e.metrics.is_some()).count(),
        with_stars: entries
            .iter()
            .filter(|e| e.metrics.map_or(false, |m| m.github_stars.is_some()))
            .count(),
        with_rust_percentage: entries
            .iter()
            .filter(|e| {
                e.metrics
                    .map_or(false, |m| m.github_rust_percentage.is_some())
            })
            .count(),
        with_downloads: entries
            .iter()
            .filter(|e| e.metrics.map_or(false, |m| m.cargo_downloads.is_some()))
            .count(),
        by_section,
    };
    fs::write(COVERAGE, serde_json::to_string_pretty(&cov)? + "\n")
        .with_context(|| format!("writing {}", COVERAGE))?;

    println!("P1 enrich  -> {}", ENRICHED);
    if !present {
        println!(
            "  NOTE {} absent -- metrics are empty. It is gitignored and filled",
            POPULARITY
        );
        println!("       by `cargo run` (the link checker); CI restores it from cache.");
    }
    println!("  entries       {}", cov.entries);
    println!(
        "  unique ids    {} ({} projects listed in >1 category)",
        cov.unique_ids, cov.multi_listed
    );
    println!("  enriched      {}", cov.enriched);
    println!("  stars         {}", cov.with_stars);
    println!("  rust pct      {}", cov.with_rust_percentage);
    println!("  downloads     {}", cov.with_downloads);
    println!("  coverage      {}", COVERAGE);
    Ok(())
}

/// A collapsed node: one real project, however many categories list it.
#[derive(Debug, Clone, Serialize, Deserialize)]
struct Node {
    id: String,
    name: String,
    /// Upstream writes display names inconsistently ("clap-rs" vs "clap-rs/clap"),
    /// so every spelling seen is kept for matching.
    names: Vec<String>,
    sections: Vec<String>,
    /// Every category this project is listed under. More than one means it
    /// genuinely spans capabilities -- that is signal, not duplication.
    category_paths: Vec<Vec<String>>,
    listings: usize,
    primary_url: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    github_url: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    crate_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    crates_io_url: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    repo: Option<Repo>,
    description: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    mentions: Vec<LinkRef>,
    lines: Vec<usize>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    metrics: Option<Metrics>,
    /// How many other curated entries link to this one: a load-bearing rank
    /// mined from the curation itself, available without any network access.
    referenced_by: usize,
}

#[derive(Debug, Clone, Serialize)]
struct Edge {
    from: String,
    to: String,
    kind: &'static str,
}

#[derive(Debug, Clone, Serialize)]
struct Category {
    id: String,
    path: Vec<String>,
    depth: usize,
    /// Direct members. Everything here is a mutual alternative, so the
    /// "alternative_to" relation is this list rather than N^2 materialised edges.
    members: Vec<String>,
    /// Members at this level and below.
    subtree_count: usize,
}

fn graph() -> Result<()> {
    fs::create_dir_all(INDEX_DIR)?;
    // Always the plain entries, never the enriched ones. nodes.jsonl is a
    // committed artifact, so it must not vary with whether the gitignored
    // popularity cache happens to exist -- otherwise CI regenerates it without
    // the cache, gets a different file, and fails a diff gate for no reason.
    // Adoption data reaches queries through downloads.json instead.
    let raw = fs::read_to_string(ENTRIES)
        .with_context(|| format!("reading {} (run `extract` first)", ENTRIES))?;
    let entries: Vec<Entry> = raw
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(serde_json::from_str)
        .collect::<Result<_, _>>()
        .context("parsing entries")?;

    // --- collapse occurrences into nodes (BTreeMap keeps output deterministic)
    let mut nodes: BTreeMap<String, Node> = BTreeMap::new();
    for e in &entries {
        let n = nodes.entry(e.id.clone()).or_insert_with(|| Node {
            id: e.id.clone(),
            name: e.name.clone(),
            names: Vec::new(),
            sections: Vec::new(),
            category_paths: Vec::new(),
            listings: 0,
            primary_url: e.primary_url.clone(),
            github_url: None,
            crate_name: None,
            crates_io_url: None,
            repo: None,
            description: String::new(),
            mentions: Vec::new(),
            lines: Vec::new(),
            metrics: None,
            referenced_by: 0,
        });
        n.listings += 1;
        if !n.names.contains(&e.name) {
            n.names.push(e.name.clone());
        }
        if !e.section.is_empty() && !n.sections.contains(&e.section) {
            n.sections.push(e.section.clone());
        }
        if !n.category_paths.contains(&e.category_path) {
            n.category_paths.push(e.category_path.clone());
        }
        n.lines.push(e.line);
        // Keep the most informative description seen across listings.
        if e.description.len() > n.description.len() {
            n.description = e.description.clone();
        }
        if n.github_url.is_none() {
            n.github_url = e.github_url.clone();
        }
        if n.crate_name.is_none() {
            n.crate_name = e.crate_name.clone();
        }
        if n.crates_io_url.is_none() {
            n.crates_io_url = e.crates_io_url.clone();
        }
        if n.repo.is_none() {
            n.repo = e.repo.clone();
        }
        if n.metrics.is_none() {
            n.metrics = e.metrics;
        }
        for m in &e.mentions {
            if !n.mentions.iter().any(|x| x.url == m.url) {
                n.mentions.push(m.clone());
            }
        }
    }
    for n in nodes.values_mut() {
        n.lines.sort_unstable();
    }

    // --- taxonomy: a category node per path prefix, with rollup counts
    let mut members: BTreeMap<String, Vec<String>> = BTreeMap::new();
    let mut subtree: BTreeMap<String, usize> = BTreeMap::new();
    let cat_id = |p: &[String]| format!("cat:{}", p.join("/"));
    for n in nodes.values() {
        for path in &n.category_paths {
            members.entry(cat_id(path)).or_default().push(n.id.clone());
            for i in 1..=path.len() {
                *subtree.entry(cat_id(&path[..i])).or_default() += 1;
            }
        }
    }
    let mut categories: Vec<Category> = subtree
        .keys()
        .map(|id| {
            let path: Vec<String> = id
                .trim_start_matches("cat:")
                .split('/')
                .map(|s| s.to_string())
                .collect();
            let mut m = members.get(id).cloned().unwrap_or_default();
            m.sort();
            m.dedup();
            Category {
                id: id.clone(),
                depth: path.len(),
                path,
                members: m,
                subtree_count: subtree[id],
            }
        })
        .collect();
    categories.sort_by(|a, b| a.id.cmp(&b.id));

    // --- edges
    let mut edges: Vec<Edge> = Vec::new();
    for n in nodes.values() {
        for path in &n.category_paths {
            edges.push(Edge {
                from: n.id.clone(),
                to: cat_id(path),
                kind: "in_category",
            });
        }
    }

    // Cross-references: a curated entry whose text links to another curated
    // entry. Both the primary and the normalised GitHub URL are resolvable.
    let mut lookup: BTreeMap<String, String> = BTreeMap::new();
    for n in nodes.values() {
        lookup.insert(n.primary_url.clone(), n.id.clone());
        if let Some(g) = &n.github_url {
            lookup.entry(g.clone()).or_insert_with(|| n.id.clone());
        }
    }
    let mut refs: BTreeSet<(String, String)> = BTreeSet::new();
    for n in nodes.values() {
        for m in &n.mentions {
            let key = normalise_github(&m.url).unwrap_or_else(|| m.url.clone());
            let target = lookup.get(&key).or_else(|| lookup.get(&m.url));
            if let Some(t) = target {
                if *t != n.id {
                    refs.insert((n.id.clone(), t.clone()));
                }
            }
        }
    }
    let mut inbound: BTreeMap<String, usize> = BTreeMap::new();
    for (from, to) in &refs {
        *inbound.entry(to.clone()).or_default() += 1;
        edges.push(Edge {
            from: from.clone(),
            to: to.clone(),
            kind: "references",
        });
    }
    for (id, c) in &inbound {
        if let Some(n) = nodes.get_mut(id) {
            n.referenced_by = *c;
        }
    }

    let mut nodes_out = String::new();
    for n in nodes.values() {
        nodes_out.push_str(&serde_json::to_string(n)?);
        nodes_out.push('\n');
    }
    fs::write(NODES, nodes_out).with_context(|| format!("writing {}", NODES))?;

    let mut edges_out = String::new();
    for e in &edges {
        edges_out.push_str(&serde_json::to_string(e)?);
        edges_out.push('\n');
    }
    fs::write(EDGES, edges_out).with_context(|| format!("writing {}", EDGES))?;
    fs::write(
        CATEGORIES,
        serde_json::to_string_pretty(&categories)? + "\n",
    )
    .with_context(|| format!("writing {}", CATEGORIES))?;

    let multi = nodes.values().filter(|n| n.listings > 1).count();
    let leaves = categories.iter().filter(|c| !c.members.is_empty()).count();
    println!("P2 graph   -> {} / {} / {}", NODES, EDGES, CATEGORIES);
    println!(
        "  nodes         {} (from {} listings)",
        nodes.len(),
        entries.len()
    );
    println!("  multi-listed  {}", multi);
    println!(
        "  categories    {} ({} with direct members)",
        categories.len(),
        leaves
    );
    println!(
        "  edges         {} in_category + {} references",
        edges.len() - refs.len(),
        refs.len()
    );
    let mut top: Vec<(&String, &usize)> = inbound.iter().collect();
    top.sort_by(|a, b| b.1.cmp(a.1).then(a.0.cmp(b.0)));
    println!("  most referenced by other entries:");
    for (id, c) in top.iter().take(8) {
        let name = nodes.get(*id).map(|n| n.name.as_str()).unwrap_or("");
        println!("    {:>3}  {:<24} {}", c, name, id);
    }
    Ok(())
}

fn main() -> Result<()> {
    let cmd = std::env::args().nth(1).unwrap_or_else(|| "all".to_string());
    match cmd.as_str() {
        "extract" => {
            extract()?;
        }
        "enrich" => {
            enrich()?;
        }
        "graph" => {
            graph()?;
        }
        "all" => {
            extract()?;
            enrich()?;
            graph()?;
        }
        other => anyhow::bail!(
            "unknown command {:?}; expected extract|enrich|graph|all",
            other
        ),
    }
    Ok(())
}
