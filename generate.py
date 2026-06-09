"""
generate.py — manuutechy GitHub Profile Card Generator
Fetches live stats via GitHub GraphQL API and writes them into profile.svg
Run via GitHub Actions on a schedule.

Required secrets:
  ACCESS_TOKEN  — Fine-grained PAT with:
                  Account: read:Followers
                  Repos:   read:Metadata, read:Contents, read:Commit statuses
  USER_NAME     — 'manuutechy'
"""

import os
import re
import time
import hashlib
import datetime
import requests
from lxml import etree
from dateutil import relativedelta

# ── Config ────────────────────────────────────────────────────────────────────

USER_NAME   = os.environ.get("USER_NAME", "manuutechy")
TOKEN       = os.environ.get("ACCESS_TOKEN", "")
HEADERS     = {"authorization": f"token {TOKEN}"}
GRAPHQL_URL = "https://api.github.com/graphql"
OWNER_ID    = None   # set after user_getter() runs
QUERY_COUNT = {"user_getter": 0, "graph_repos": 0, "graph_commits": 0, "loc_query": 0, "follower_getter": 0}

# ── Helpers ───────────────────────────────────────────────────────────────────

def query_count(key):
    QUERY_COUNT[key] += 1


def gql(func_name, query, variables):
    """POST a GraphQL request; raise on non-200."""
    r = requests.post(GRAPHQL_URL, json={"query": query, "variables": variables}, headers=HEADERS)
    if r.status_code == 200:
        return r
    raise Exception(f"{func_name} failed: {r.status_code} — {r.text}")


def perf(func, *args):
    t0 = time.perf_counter()
    result = func(*args)
    return result, time.perf_counter() - t0


def fmt_time(label, diff):
    if diff >= 1:
        print(f"  {label:<22} {diff:.4f} s")
    else:
        print(f"  {label:<22} {diff*1000:.2f} ms")


def plural(n):
    return "s" if n != 1 else ""


# ── GitHub API calls ──────────────────────────────────────────────────────────

def user_getter(username):
    """Returns (owner_id_dict, created_at_str)."""
    query_count("user_getter")
    q = """
    query($login: String!) {
      user(login: $login) {
        id
        createdAt
      }
    }"""
    r = gql("user_getter", q, {"login": username})
    data = r.json()["data"]["user"]
    return {"id": data["id"]}, data["createdAt"]


def follower_getter(username):
    """Returns follower count."""
    query_count("follower_getter")
    q = """
    query($login: String!) {
      user(login: $login) {
        followers { totalCount }
      }
    }"""
    r = gql("follower_getter", q, {"login": username})
    return r.json()["data"]["user"]["followers"]["totalCount"]


def graph_repos(count_type, affiliations, cursor=None):
    """Returns repo count or star total depending on count_type."""
    query_count("graph_repos")
    q = """
    query($affiliations: [RepositoryAffiliation], $login: String!, $cursor: String) {
      user(login: $login) {
        repositories(first: 100, after: $cursor, ownerAffiliations: $affiliations) {
          totalCount
          edges {
            node {
              ... on Repository {
                nameWithOwner
                stargazers { totalCount }
              }
            }
          }
          pageInfo { endCursor hasNextPage }
        }
      }
    }"""
    r = gql("graph_repos", q, {"affiliations": affiliations, "login": USER_NAME, "cursor": cursor})
    repos = r.json()["data"]["user"]["repositories"]

    if count_type == "repos":
        return repos["totalCount"]
    elif count_type == "stars":
        total = sum(e["node"]["stargazers"]["totalCount"] for e in repos["edges"])
        if repos["pageInfo"]["hasNextPage"]:
            total += graph_repos("stars", affiliations, repos["pageInfo"]["endCursor"])
        return total


def graph_commits(start_date, end_date):
    """Returns total contributions in the given date range."""
    query_count("graph_commits")
    q = """
    query($start: DateTime!, $end: DateTime!, $login: String!) {
      user(login: $login) {
        contributionsCollection(from: $start, to: $end) {
          contributionCalendar { totalContributions }
        }
      }
    }"""
    r = gql("graph_commits", q, {"start": start_date, "end": end_date, "login": USER_NAME})
    return r.json()["data"]["user"]["contributionsCollection"]["contributionCalendar"]["totalContributions"]


def total_commits(acc_date):
    """Sums commits year-by-year from account creation to now."""
    total = 0
    start = datetime.datetime.strptime(acc_date[:10], "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    end   = datetime.datetime.now(datetime.timezone.utc)

    year_start = start
    while year_start < end:
        year_end = min(year_start.replace(year=year_start.year + 1), end)
        total += graph_commits(
            year_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            year_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        year_start = year_end
    return total


# ── LOC (lines of code) via cache ─────────────────────────────────────────────

def loc_query(affiliations, cursor=None, edges=None):
    """Fetches all repo names + commit counts for LOC calculation."""
    query_count("loc_query")
    if edges is None:
        edges = []
    q = """
    query($affiliations: [RepositoryAffiliation], $login: String!, $cursor: String) {
      user(login: $login) {
        repositories(first: 60, after: $cursor, ownerAffiliations: $affiliations) {
          edges {
            node {
              ... on Repository {
                nameWithOwner
                defaultBranchRef {
                  target {
                    ... on Commit { history { totalCount } }
                  }
                }
              }
            }
          }
          pageInfo { endCursor hasNextPage }
        }
      }
    }"""
    r = gql("loc_query", q, {"affiliations": affiliations, "login": USER_NAME, "cursor": cursor})
    data = r.json()["data"]["user"]["repositories"]
    edges += data["edges"]

    if data["pageInfo"]["hasNextPage"]:
        return loc_query(affiliations, data["pageInfo"]["endCursor"], edges)
    return cache_builder(edges)


def recursive_loc(owner, repo, cursor=None, adds=0, dels=0, my_commits=0):
    """Paginates through a repo's commits to count lines authored by me."""
    q = """
    query($repo: String!, $owner: String!, $cursor: String) {
      repository(name: $repo, owner: $owner) {
        defaultBranchRef {
          target {
            ... on Commit {
              history(first: 100, after: $cursor) {
                totalCount
                edges {
                  node {
                    ... on Commit { committedDate }
                    author { user { id } }
                    deletions
                    additions
                  }
                }
                pageInfo { endCursor hasNextPage }
              }
            }
          }
        }
      }
    }"""
    r = requests.post(GRAPHQL_URL, json={"query": q, "variables": {"repo": repo, "owner": owner, "cursor": cursor}}, headers=HEADERS)
    if r.status_code != 200:
        return adds, dels, my_commits

    ref = r.json()["data"]["repository"]["defaultBranchRef"]
    if not ref:
        return adds, dels, my_commits

    history = ref["target"]["history"]
    for edge in history["edges"]:
        if edge["node"]["author"]["user"] == OWNER_ID:
            my_commits += 1
            adds += edge["node"]["additions"]
            dels += edge["node"]["deletions"]

    if history["pageInfo"]["hasNextPage"]:
        return recursive_loc(owner, repo, history["pageInfo"]["endCursor"], adds, dels, my_commits)
    return adds, dels, my_commits


def cache_builder(edges):
    """Builds / updates a cache file of LOC per repo; returns [add, del, net]."""
    os.makedirs("cache", exist_ok=True)
    filename = "cache/" + hashlib.sha256(USER_NAME.encode()).hexdigest() + ".txt"

    try:
        with open(filename, "r") as f:
            cached = {line.split()[0]: line.split() for line in f if line.strip()}
    except FileNotFoundError:
        cached = {}

    updated = {}
    for edge in edges:
        node = edge["node"]
        name = node["nameWithOwner"]
        h    = hashlib.sha256(name.encode()).hexdigest()

        try:
            commit_count = node["defaultBranchRef"]["target"]["history"]["totalCount"]
        except (TypeError, KeyError):
            commit_count = 0

        if h in cached and int(cached[h][1]) == commit_count:
            updated[h] = cached[h]
        else:
            owner, repo = name.split("/", 1)
            try:
                a, d, c = recursive_loc(owner, repo)
            except Exception:
                a, d, c = 0, 0, 0
            updated[h] = [h, str(commit_count), str(c), str(a), str(d)]

    with open(filename, "w") as f:
        for row in updated.values():
            f.write(" ".join(row) + "\n")

    loc_add = sum(int(v[3]) for v in updated.values())
    loc_del = sum(int(v[4]) for v in updated.values())
    return [loc_add, loc_del, loc_add - loc_del]


# ── SVG generation ─────────────────────────────────────────────────────────────

SVG_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<svg width="860" height="280" viewBox="0 0 860 280"
     xmlns="http://www.w3.org/2000/svg"
     xmlns:xlink="http://www.w3.org/1999/xlink">
  <defs>
    <style>
      @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&amp;display=swap');
      text {{ font-family: 'JetBrains Mono', monospace; }}
    </style>
    <!-- Dark BG -->
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%"   stop-color="#0d0d0d"/>
      <stop offset="100%" stop-color="#111111"/>
    </linearGradient>
    <!-- Orange accent line -->
    <linearGradient id="accent" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%"   stop-color="#ff6b00"/>
      <stop offset="100%" stop-color="#ff6b0000"/>
    </linearGradient>
  </defs>

  <!-- Background -->
  <rect width="860" height="280" rx="12" fill="url(#bg)"/>

  <!-- Left orange accent bar -->
  <rect x="0" y="0" width="4" height="280" rx="2" fill="#ff6b00"/>

  <!-- Name -->
  <text x="36" y="52" font-size="28" font-weight="700" fill="#ffffff" letter-spacing="-0.5">manuutechy</text>

  <!-- Tagline -->
  <text x="36" y="78" font-size="13" fill="#888888">backend engineer · fintech builder · manuutech.com</text>

  <!-- Divider -->
  <rect x="36" y="96" width="788" height="1" fill="url(#accent)"/>

  <!-- Stat labels (row 1) -->
  <text x="36"  y="136" font-size="11" fill="#555555" letter-spacing="1">COMMITS</text>
  <text x="196" y="136" font-size="11" fill="#555555" letter-spacing="1">REPOS</text>
  <text x="356" y="136" font-size="11" fill="#555555" letter-spacing="1">STARS</text>
  <text x="516" y="136" font-size="11" fill="#555555" letter-spacing="1">FOLLOWERS</text>
  <text x="676" y="136" font-size="11" fill="#555555" letter-spacing="1">LINES WRITTEN</text>

  <!-- Stat values (row 1) -->
  <text id="commit_data"   x="36"  y="166" font-size="26" font-weight="700" fill="#ffffff">…</text>
  <text id="repo_data"     x="196" y="166" font-size="26" font-weight="700" fill="#ffffff">…</text>
  <text id="star_data"     x="356" y="166" font-size="26" font-weight="700" fill="#ffffff">…</text>
  <text id="follower_data" x="516" y="166" font-size="26" font-weight="700" fill="#ffffff">…</text>
  <text id="loc_data"      x="676" y="166" font-size="26" font-weight="700" fill="#ff6b00">…</text>

  <!-- Sub-stats (LOC breakdown) -->
  <text x="676" y="186" font-size="10" fill="#555555">
    <tspan id="loc_add" fill="#3fb950">+…</tspan>
    <tspan>  </tspan>
    <tspan id="loc_del" fill="#f85149">-…</tspan>
  </text>

  <!-- Divider 2 -->
  <rect x="36" y="204" width="788" height="1" fill="url(#accent)" opacity="0.4"/>

  <!-- Footer line -->
  <text x="36"  y="232" font-size="11" fill="#444444">stack</text>
  <text x="36"  y="252" font-size="11" fill="#666666">Laravel · Flutter · PostgreSQL · Redis · FastAPI · NestJS · Next.js · Docker · AWS</text>

  <!-- Last updated -->
  <text id="updated_at" x="824" y="268" font-size="10" fill="#333333" text-anchor="end">updated …</text>
</svg>
"""


def svg_overwrite(filename, commit_data, star_data, repo_data, follower_data, loc_data):
    """Parse the SVG and inject live values."""
    tree = etree.parse(filename)
    root = tree.getroot()

    def set_text(el_id, text):
        el = root.find(f".//*[@id='{el_id}']")
        if el is not None:
            el.text = str(text)

    set_text("commit_data",   f"{commit_data:,}")
    set_text("repo_data",     f"{repo_data:,}")
    set_text("star_data",     f"{star_data:,}")
    set_text("follower_data", f"{follower_data:,}")
    set_text("loc_data",      f"{loc_data[2]:,}")
    set_text("loc_add",       f"+{loc_data[0]:,}")
    set_text("loc_del",       f"-{loc_data[1]:,}")
    set_text("updated_at",    "updated " + datetime.datetime.utcnow().strftime("%d %b %Y"))

    tree.write(filename, encoding="utf-8", xml_declaration=True)
    print(f"  ✓ wrote {filename}")


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Fetching stats for @manuutechy …\n")
    print("Calculation times:")

    # User metadata
    (user_data, acc_date), t = perf(user_getter, USER_NAME)
    OWNER_ID = user_data
    fmt_time("account data", t)

    # Commits (all years)
    commit_data, t = perf(total_commits, acc_date)
    fmt_time("commits", t)

    # Repos + stars
    repo_data,     t1 = perf(graph_repos, "repos",  ["OWNER"])
    star_data,     t2 = perf(graph_repos, "stars",  ["OWNER"])
    follower_data, t3 = perf(follower_getter, USER_NAME)
    fmt_time("repos / stars / followers", t1 + t2 + t3)

    # Lines of code (cached per repo)
    loc_data, t = perf(loc_query, ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"])
    fmt_time("lines of code (cached)", t)

    # Write SVG
    svg_path = "profile.svg"
    with open(svg_path, "w", encoding="utf-8") as f:
        f.write(SVG_TEMPLATE)

    svg_overwrite(svg_path, commit_data, star_data, repo_data, follower_data, loc_data)

    print(f"\nStats:")
    print(f"  commits   {commit_data:,}")
    print(f"  repos     {repo_data:,}")
    print(f"  stars     {star_data:,}")
    print(f"  followers {follower_data:,}")
    print(f"  LOC net   {loc_data[2]:,}  (+{loc_data[0]:,} / -{loc_data[1]:,})")

    print(f"\nAPI calls:")
    for k, v in QUERY_COUNT.items():
        print(f"  {k:<22} {v}")
