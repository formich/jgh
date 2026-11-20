#!/usr/bin/env python3
"""jgh - keep Jira issues and GitHub pull requests in sync.

Maps every Jira issue assigned to you to the GitHub PRs whose branch name
carries its key (PROJ-123/..., PROJ-123-..., feature/PROJ-123-...), then reports:

  * issues still in a developer status (e.g. To Do / In Progress / In Review)
    whose PRs are all merged into the repo's default branch -> propose the
    hand-over status (e.g. "Testing")
  * issues already handed over that still have open PRs, or no PR at all
  * PRs whose branch carries no issue key

Stdlib only, Python >= 3.9. GitHub via GraphQL (token from `gh auth token`
or $GH_TOKEN), Jira via REST API v3 (token from $JIRA_API_TOKEN).
"""

import argparse
import base64
import datetime as dt
import http.client
import json
import os
import re
import subprocess
import sys
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

__version__ = "0.1.0"

CONFIG_DIR = os.path.expanduser(os.environ.get("JGH_HOME", "~/.config/jgh"))
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
OVERRIDES_FILE = os.path.join(CONFIG_DIR, "overrides.json")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")

DEFAULT_CONFIG = {
    # Your Jira site, e.g. "https://your-company.atlassian.net" (required).
    "jira_base_url": "",
    # API base; leave empty to use jira_base_url. For a *scoped* API token use
    # "https://api.atlassian.com/ex/jira/<cloudId>".
    "jira_api_base": "",
    "jira_email": "",
    # Jira project keys to track, e.g. ["PROJ"]. Empty = any key in branch names.
    "project_keys": [],
    # GitHub orgs/users to search PRs in. Empty = all PRs you authored.
    "github_owners": [],
    "pr_lookback_days": 90,
    # Also print the Jira URL in plain text under each issue (for terminals or
    # tmux setups that don't render OSC 8 hyperlinks).
    "plain_urls": False,
    # Jira labels that mark an issue as "no code needed" (analysis, meetings, config...).
    "no_code_labels": ["no-code"],
    # Issues outside your JQL (e.g. Done) linked only to merged PRs are not
    # re-fetched while their cached status is terminal and younger than this.
    "terminal_statuses": ["Done"],
    "issue_cache_hours": 72,
    # Statuses where the issue is still the developer's responsibility.
    "dev_statuses": ["To Do", "In Progress", "In Review"],
    # Ordered path the tool walks to reach the target status. Statuses before an
    # issue's target count as the developer's too.
    "workflow_path": ["To Do", "In Progress", "In Review", "Testing", "Done"],
    # Hand-over status: where an issue goes once all its PRs are merged.
    "target_status": "Testing",
    # Per issue type target, e.g. {"Story": "Done"} for types that skip QA.
    "target_status_by_type": {},
    # Statuses at or past the hand-over.
    "handed_over_statuses": ["Testing", "Done"],
    # Branch keys that are placeholders, not real issues (PROJ-0, PROJ-000, ...).
    "placeholder_key_regex": r"^[A-Z][A-Z0-9]+-0+$",
}

DEFAULT_OVERRIDES = {
    # "PROJ-123": ["my-org/my-repo#42"]  -> extra PRs for an issue
    "links": {},
    # PRs to drop from an issue even if the branch carries its key
    "unlinks": {},
    # issue keys / PR refs to hide entirely
    "ignore": [],
    # issue keys that need no code: no "missing PR" warning, moved on demand
    "no_code": [],
    # "PROJ-123": "Done"  -> per-issue target status
    "targets": {},
}


# --------------------------------------------------------------------------- io


def _load_json(path: str, default: dict) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return json.loads(json.dumps(default))
    merged = json.loads(json.dumps(default))
    merged.update(data)
    return merged


def _save_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def load_config() -> dict:
    cfg = _load_json(CONFIG_FILE, DEFAULT_CONFIG)
    cfg["jira_base_url"] = os.environ.get("JIRA_BASE_URL", cfg["jira_base_url"]).rstrip("/")
    cfg["jira_email"] = os.environ.get("JIRA_EMAIL", cfg["jira_email"])
    cfg["jira_api_base"] = (
        os.environ.get("JIRA_API_BASE") or cfg.get("jira_api_base") or cfg["jira_base_url"]
    ).rstrip("/")
    return cfg


def load_overrides() -> dict:
    return _load_json(OVERRIDES_FILE, DEFAULT_OVERRIDES)


# ------------------------------------------------------------------------ color

_TTY = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def link(text: str, url: str) -> str:
    """OSC 8 hyperlink, underlined so it reads as clickable (cmd/ctrl+click)."""
    if not (_TTY and url):
        return text
    return f"\033]8;;{url}\033\\\033[4m{text}\033[24m\033]8;;\033\\"


def die(msg: str, code: int = 1) -> None:
    print(c("error: ", "31;1") + msg, file=sys.stderr)
    sys.exit(code)


# ------------------------------------------------------------------------- jira


class Jira:
    def __init__(self, cfg: dict):
        if not cfg["jira_base_url"]:
            die(f"jira_base_url is not set: run `jgh init` or edit {CONFIG_FILE}.")
        token = os.environ.get("JIRA_API_TOKEN")
        if not token:
            die("JIRA_API_TOKEN is not set in the environment.")
        if not cfg["jira_email"]:
            die(f"jira_email is empty: set it in {CONFIG_FILE} or export JIRA_EMAIL.")
        self.base = cfg["jira_base_url"]  # site URL, used for browse links
        self.api = cfg["jira_api_base"]
        raw = f"{cfg['jira_email']}:{token}".encode()
        self._auth = "Basic " + base64.b64encode(raw).decode()
        self._conn = None
        self._lock = threading.Lock()

    def _connect(self) -> http.client.HTTPConnection:
        u = urllib.parse.urlsplit(self.api)
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        return cls(u.netloc, timeout=30)

    def _req(self, method: str, path: str, body: Optional[dict] = None):
        """One persistent (keep-alive) connection: saves a TLS handshake per call."""
        full = urllib.parse.urlsplit(self.api).path.rstrip("/") + path
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": self._auth, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        with self._lock:
            for attempt in (1, 2):
                if self._conn is None:
                    self._conn = self._connect()
                try:
                    self._conn.request(method, full, body=data, headers=headers)
                    resp = self._conn.getresponse()
                    code, payload = resp.status, resp.read()
                    break
                except (http.client.HTTPException, ConnectionError) as e:
                    # stale keep-alive socket: reconnect once
                    self._conn.close()
                    self._conn = None
                    if attempt == 2:
                        die(f"cannot reach Jira at {self.api}: {e}")
                except OSError as e:
                    self._conn.close()
                    self._conn = None
                    die(f"cannot reach Jira at {self.api}: {e}")
        if code == 401:
            die(
                f"Jira {method} {self.api}{path} -> HTTP 401. Check JIRA_API_TOKEN and jira_email.\n"
                f"  For a *scoped* API token set in {CONFIG_FILE}:\n"
                f'  "jira_api_base": "https://api.atlassian.com/ex/jira/<cloudId>"\n'
                + payload.decode(errors="replace")[:500]
            )
        if code >= 400:
            detail = payload.decode(errors="replace")[:500]
            raise RuntimeError(f"Jira {method} {path} -> HTTP {code}: {detail}")
        return json.loads(payload) if payload else None

    def myself(self) -> dict:
        return self._req("GET", "/rest/api/3/myself")

    def search(self, jql: str, fields: List[str]) -> List[dict]:
        out, token = [], None
        while True:
            body = {"jql": jql, "fields": fields, "maxResults": 100}
            if token:
                body["nextPageToken"] = token
            res = self._req("POST", "/rest/api/3/search/jql", body)
            out.extend(res.get("issues", []))
            token = res.get("nextPageToken")
            if not token:
                return out

    def bulk_fetch(self, keys: List[str], fields: List[str]) -> List[dict]:
        """Fetch issues by key; unknown keys are silently skipped."""
        out = []
        for i in range(0, len(keys), 100):
            chunk = keys[i : i + 100]
            res = self._req(
                "POST", "/rest/api/3/issue/bulkfetch", {"issueIdsOrKeys": chunk, "fields": fields}
            )
            out.extend(res.get("issues", []))
        return out

    def transitions(self, key: str) -> List[dict]:
        return self._req("GET", f"/rest/api/3/issue/{key}/transitions")["transitions"]

    def transition(self, key: str, transition_id: str) -> None:
        self._req("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": transition_id}})


ISSUE_FIELDS = ["summary", "status", "issuetype", "assignee", "labels"]


def simplify_issue(raw: dict, base_url: str) -> dict:
    f = raw.get("fields", {})
    return {
        "key": raw["key"],
        "summary": f.get("summary") or "",
        "status": (f.get("status") or {}).get("name", "?"),
        "type": (f.get("issuetype") or {}).get("name", "?"),
        "assignee": ((f.get("assignee") or {}).get("displayName")) or "",
        "labels": f.get("labels") or [],
        "url": f"{base_url}/browse/{raw['key']}",
    }


# ----------------------------------------------------------------------- github

PR_FIELDS = """
  number title url state isDraft headRefName baseRefName mergedAt updatedAt
  repository { nameWithOwner defaultBranchRef { name } }
"""

SEARCH_PAGE = """
  pageInfo { hasNextPage endCursor }
  nodes { ... on PullRequest { %s } }
""" % PR_FIELDS


class _GitHub:
    """Direct GraphQL over keep-alive HTTPS connections (one per thread).

    The token comes from $GH_TOKEN / $GITHUB_TOKEN or `gh auth token` (fast, no
    `gh api` process per request) and is kept in memory only.
    """

    def __init__(self):
        self._token: Optional[str] = None
        self._token_lock = threading.Lock()
        self._local = threading.local()

    def _get_token(self) -> str:
        with self._token_lock:
            if self._token is None:
                tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
                if not tok:
                    try:
                        res = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=15)
                    except FileNotFoundError:
                        die("`gh` not found on PATH (needed for the GitHub token), or set GH_TOKEN.")
                    if res.returncode != 0 or not res.stdout.strip():
                        die(f"`gh auth token` failed: {res.stderr.strip()} (run `gh auth login`).")
                    tok = res.stdout.strip()
                self._token = tok
            return self._token

    def graphql(self, query: str) -> dict:
        body = json.dumps({"query": query}).encode()
        headers = {
            "Authorization": f"bearer {self._get_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"jgh/{__version__}",
        }
        for attempt in (1, 2):
            conn = getattr(self._local, "conn", None)
            if conn is None:
                conn = self._local.conn = http.client.HTTPSConnection("api.github.com", timeout=60)
            try:
                conn.request("POST", "/graphql", body=body, headers=headers)
                resp = conn.getresponse()
                code, payload = resp.status, resp.read()
                break
            except (http.client.HTTPException, OSError) as e:
                conn.close()
                self._local.conn = None
                if attempt == 2:
                    die(f"cannot reach GitHub: {e}")
        if code == 401:
            die("GitHub -> HTTP 401: invalid token (run `gh auth login` or check GH_TOKEN).")
        if code >= 400:
            die(f"GitHub GraphQL -> HTTP {code}: {payload.decode(errors='replace')[:500]}")
        res = json.loads(payload)
        if res.get("errors") and not res.get("data"):
            die("GitHub GraphQL: " + "; ".join(e.get("message", "?") for e in res["errors"]))
        return res["data"]


_GH = _GitHub()


def gh_graphql(query: str) -> dict:
    return _GH.graphql(query)


def _pr_from_node(n: dict) -> dict:
    repo = n["repository"]["nameWithOwner"]
    return {
        "ref": f"{repo}#{n['number']}",
        "repo": repo,
        "number": n["number"],
        "title": n["title"],
        "url": n["url"],
        "state": n["state"],  # OPEN | MERGED | CLOSED
        "draft": n["isDraft"],
        "branch": n["headRefName"],
        "base": n["baseRefName"],
        "default_branch": (n["repository"].get("defaultBranchRef") or {}).get("name") or "main",
        "merged_at": n["mergedAt"],
        "updated_at": n["updatedAt"],
    }


_REF_RE = re.compile(r"^([\w.-]+)/([\w.-]+)#(\d+)$")


def fetch_prs(cfg: dict, extra_refs: List[str]) -> List[dict]:
    """All my PRs (recently updated + every open one) plus `extra_refs`."""
    since = (dt.date.today() - dt.timedelta(days=cfg["pr_lookback_days"])).isoformat()
    owners = " ".join(f"org:{o}" for o in cfg["github_owners"])  # multiple org: are OR-ed
    owners = f" {owners}" if owners else ""
    searches = {
        "recent": f"is:pr author:@me{owners} updated:>={since}",
        "open": f"is:pr author:@me{owners} is:open",  # stale open PRs too
    }
    # GitHub runs the searches of one request serially: send each as its own
    # request, in parallel, each on its own keep-alive connection.
    requests = {
        alias: f"query {{ {alias}: search(query: {json.dumps(q)}, type: ISSUE, first: 100) {{{SEARCH_PAGE}}} }}"
        for alias, q in searches.items()
    }
    refs = [r for r in dict.fromkeys(extra_refs) if _REF_RE.match(r)]
    if refs:
        parts = []
        for idx, ref in enumerate(refs):
            owner, name, num = _REF_RE.match(ref).groups()
            parts.append(
                f"x{idx}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) "
                f"{{ pullRequest(number: {num}) {{ {PR_FIELDS} }} }}"
            )
        requests["refs"] = "query { " + "\n".join(parts) + " }"
    _GH._get_token()  # resolve once, before the threads start
    data: Dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        for part in pool.map(gh_graphql, requests.values()):
            data.update(part or {})

    prs: Dict[str, dict] = {}
    for alias, q in searches.items():
        page = data[alias]
        while True:
            for n in page["nodes"]:
                if n:
                    pr = _pr_from_node(n)
                    prs[pr["ref"]] = pr
            if not page["pageInfo"]["hasNextPage"]:
                break  # >100 results is rare: follow-up pages only then
            cursor = json.dumps(page["pageInfo"]["endCursor"])
            page = gh_graphql(
                f"query {{ s: search(query: {json.dumps(q)}, type: ISSUE, "
                f"first: 100, after: {cursor}) {{{SEARCH_PAGE}}} }}"
            )["s"]
    for idx, ref in enumerate(refs):
        node = (data.get(f"x{idx}") or {}).get("pullRequest")
        if node:
            prs.setdefault(ref, _pr_from_node(node))
    return list(prs.values())


# ---------------------------------------------------------------------- mapping


def extract_key(branch: str, cfg: dict) -> Optional[str]:
    """Issue key from a branch name: PROJ-123/x, PROJ-123-x, feature/PROJ-123_x."""
    if cfg["project_keys"]:
        projects = "|".join(re.escape(p) for p in cfg["project_keys"])
        flags = re.IGNORECASE
    else:
        projects, flags = r"[A-Z][A-Z0-9]+", 0  # any key: uppercase only, fewer false hits
    m = re.match(rf"^(?:[\w.-]+/)?({projects})-(\d+)(?=$|[/_\-.])", branch, flags)
    if not m:
        return None
    key = f"{m.group(1).upper()}-{m.group(2)}"
    if re.match(cfg["placeholder_key_regex"], key):
        return None
    return key


def pr_is_done(pr: dict) -> bool:
    return pr["state"] == "MERGED" and pr["base"] == pr["default_branch"]


def target_of(issue: dict, cfg: dict) -> str:
    """Target status of an issue: per-issue override, then per type, then default."""
    return (
        issue.get("target")
        or (cfg.get("target_status_by_type") or {}).get(issue.get("type", ""))
        or cfg["target_status"]
    )


def owned_statuses(target: str, cfg: dict) -> List[str]:
    """Statuses in which an issue with this target is still the developer's."""
    owned = list(cfg["dev_statuses"])
    path = cfg["workflow_path"]
    if target in path:
        owned += [s for s in path[: path.index(target)] if s not in owned]
    return owned


def classify(issue: dict, prs: List[dict], cfg: dict) -> str:
    """Return one of: move, nocode, wip, warn_open, warn_noprs, ok, other, ignore."""
    live = [p for p in prs if p["state"] != "CLOSED"]  # closed-unmerged PRs don't count
    status = issue["status"]
    target = target_of(issue, cfg)
    owned = owned_statuses(target, cfg)
    if issue.get("no_code") and not live:
        # No PR to watch: the developer decides when it's done (`jgh sync KEY`).
        if status in owned:
            return "nocode"
        return "ok" if status in cfg["handed_over_statuses"] or status == target else "ignore"
    if status in owned:
        if live and all(pr_is_done(p) for p in live):
            return "move"
        return "wip"
    if status in cfg["handed_over_statuses"] or status == target:
        if any(p["state"] == "OPEN" for p in live):
            return "warn_open"
        if status == target and not live and target not in cfg.get("terminal_statuses", []):
            return "warn_noprs"
        return "ok"
    if any(p["state"] == "OPEN" or pr_is_done(p) for p in live):
        return "other"  # e.g. backlog / on hold with code attached
    return "ignore"


def _jql_list(values: List[str]) -> str:
    return ", ".join(json.dumps(v) for v in values)


def my_issues_jql(cfg: dict) -> str:
    target, days = cfg["target_status"], cfg["pr_lookback_days"]
    clauses = ["assignee = currentUser()"]
    if cfg["project_keys"]:
        clauses.append(f"project in ({_jql_list(cfg['project_keys'])})")
    owned = owned_statuses(target, cfg)
    parts = [f"status in ({_jql_list(owned)})"]
    # Issue types with a later target are still owned in some extra statuses.
    by_target: Dict[str, List[str]] = {}
    for itype, t in sorted((cfg.get("target_status_by_type") or {}).items()):
        by_target.setdefault(t, []).append(itype)
    for t, types in sorted(by_target.items()):
        extra = [s for s in owned_statuses(t, cfg) if s not in owned]
        if extra:
            parts.append(f"(issuetype in ({_jql_list(types)}) AND status in ({_jql_list(extra)}))")
    parts.append(f"(status = {json.dumps(target)} AND status CHANGED TO {json.dumps(target)} AFTER -{days}d)")
    clauses.append("(" + " OR ".join(parts) + ")")
    return " AND ".join(clauses)


def _load_issue_cache() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f).get("issue_cache", {})
    except (FileNotFoundError, ValueError):
        return {}


def annotate(issue: dict, cfg: dict, ov: dict) -> None:
    """Add the local flags: no_code (override or label) and target status."""
    key = issue["key"]
    labels = set(cfg.get("no_code_labels") or [])
    issue["no_code"] = key in ov.get("no_code", []) or bool(labels & set(issue.get("labels", [])))
    issue.pop("target", None)
    issue["target"] = (ov.get("targets") or {}).get(key) or target_of(issue, cfg)


def build_mapping(cfg: dict, jira: "Jira", ov: dict, refresh: bool = False) -> dict:
    extra_refs = [r for refs in ov.get("links", {}).values() for r in refs]
    # GitHub and Jira are independent: query them in parallel.
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_prs = pool.submit(fetch_prs, cfg, extra_refs)
        f_issues = pool.submit(jira.search, my_issues_jql(cfg), ISSUE_FIELDS)
        prs, raw_issues = f_prs.result(), f_issues.result()
    ignore = set(ov.get("ignore", []))
    pr_by_ref = {p["ref"]: p for p in prs}

    by_key: Dict[str, List[dict]] = {}
    unmapped: List[dict] = []
    for p in prs:
        if p["ref"] in ignore:
            continue
        key = extract_key(p["branch"], cfg)
        if key:
            by_key.setdefault(key, []).append(p)
        elif p["state"] != "CLOSED":
            unmapped.append(p)

    for key, refs in ov.get("links", {}).items():
        for ref in refs:
            pr = pr_by_ref.get(ref)
            if pr and pr not in by_key.setdefault(key, []):
                by_key[key].append(pr)
            unmapped = [u for u in unmapped if u["ref"] != ref]
    for key, refs in ov.get("unlinks", {}).items():
        if key in by_key:
            by_key[key] = [p for p in by_key[key] if p["ref"] not in refs]

    issues = {i["key"]: simplify_issue(i, jira.base) for i in raw_issues}

    # Keys seen only in branches: reuse cached terminal statuses (Done...) when
    # nothing open is attached, fetch the rest in one bulk call.
    cache = {} if refresh else _load_issue_cache()
    now = dt.datetime.now()
    ttl = dt.timedelta(hours=cfg.get("issue_cache_hours", 72))
    terminal = set(cfg.get("terminal_statuses", []))
    missing = []
    targets = ov.get("targets") or {}
    for k in sorted(set(by_key) | set(targets)):
        if k in issues:
            continue
        hit = cache.get(k)
        fresh = hit and now - dt.datetime.fromisoformat(hit["fetched_at"]) < ttl
        no_open = all(p["state"] != "OPEN" for p in by_key.get(k, []))
        if fresh and hit["issue"]["status"] in terminal and no_open:
            issues[k] = dict(hit["issue"], cached=True)
        else:
            missing.append(k)
    for raw in jira.bulk_fetch(missing, ISSUE_FIELDS):
        i = simplify_issue(raw, jira.base)
        issues[i["key"]] = i
    new_cache = {
        k: (cache[k] if i.get("cached") else {"fetched_at": now.isoformat(timespec="seconds"), "issue": i})
        for k, i in issues.items()
    }

    rows = []
    for key, issue in issues.items():
        if key in ignore:
            continue
        annotate(issue, cfg, ov)
        linked = sorted(by_key.get(key, []), key=lambda p: (p["repo"], p["number"]))
        rows.append({"issue": issue, "prs": linked, "verdict": classify(issue, linked, cfg)})
    unknown = sorted(k for k in by_key if k not in issues)
    return {
        "rows": rows,
        "unmapped": unmapped,
        "unknown_keys": unknown,
        "prs_by_key": by_key,
        "issue_cache": new_cache,
    }


def save_state(mapping: dict) -> None:
    state = {
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "issues": {
            r["issue"]["key"]: {
                "status": r["issue"]["status"],
                "summary": r["issue"]["summary"],
                "verdict": r["verdict"],
                "prs": [
                    {"ref": p["ref"], "branch": p["branch"], "state": p["state"], "merged_at": p["merged_at"]}
                    for p in r["prs"]
                ],
            }
            for r in mapping["rows"]
        },
        "unmapped_prs": [p["ref"] for p in mapping["unmapped"]],
        "issue_cache": mapping.get("issue_cache", {}),
    }
    _save_json(STATE_FILE, state)


# ---------------------------------------------------------------------- display


def sections(cfg: dict, move_targets: Optional[set] = None) -> List[tuple]:
    t = cfg["target_status"]
    targets = move_targets or {t}
    ready = f"Ready for {next(iter(targets))}" if len(targets) == 1 else "Ready to move"
    return [
        ("move", f"{ready} (all PRs merged)", "33;1"),
        ("warn_open", f"Already in {t} or later, but PRs still open", "31;1"),
        ("warn_noprs", f"In {t} with no PR found", "35;1"),
        ("other", "Not in a dev status, but code attached", "36;1"),
        ("nocode", "No code needed — `jgh sync KEY` when done", "36;1"),
        ("wip", "In progress", "34;1"),
        ("ok", "In sync", "32;1"),
    ]


def fmt_pr(p: dict) -> str:
    if p["state"] == "MERGED":
        mark = c("✓", "32") if pr_is_done(p) else c(f"✓→{p['base']}", "33")
    elif p["state"] == "OPEN":
        mark = c("draft", "90") if p["draft"] else c("open", "33")
    else:
        mark = c("closed", "90")
    repo = p["repo"].split("/", 1)[1]
    return link(f"{repo}#{p['number']}", p["url"]) + " " + mark


def trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def print_report(mapping: dict, cfg: dict, show_all: bool, plain_urls: bool = False) -> None:
    rows = mapping["rows"]
    default = cfg["target_status"]
    move_targets = {target_of(r["issue"], cfg) for r in rows if r["verdict"] == "move"}
    for verdict, title, color in sections(cfg, move_targets):
        group = [r for r in rows if r["verdict"] == verdict]
        if not group or (verdict == "ok" and not show_all):
            continue
        print(c(f"\n{title} ({len(group)})", color))
        for r in sorted(group, key=lambda r: r["issue"]["key"]):
            i = r["issue"]
            empty = "no code needed" if i.get("no_code") else "no PR"
            prs = ", ".join(fmt_pr(p) for p in r["prs"]) or c(empty, "90")
            target = target_of(i, cfg)
            if target != default and verdict in ("move", "nocode", "wip"):
                prs += c(f"  → {target}", "36")
            key = link(i["key"], i["url"]) + " " * max(1, 11 - len(i["key"]))
            print(f"  {key}{c(i['status'].ljust(16), '1')} {trunc(i['summary'], 60)}")
            print(f"  {'':11}{'':16} └ {prs}")
            if plain_urls:
                print(f"  {'':11}{'':16}   {c(i['url'], '90')}")
    if mapping["unmapped"]:
        print(c(f"\nPRs with no issue key in the branch ({len(mapping['unmapped'])})", "90;1"))
        for p in mapping["unmapped"]:
            print(f"  {fmt_pr(p)}  {c(p['branch'], '90')}  {trunc(p['title'], 60)}")
    if mapping["unknown_keys"]:
        print(c("\nBranch keys not found in Jira: ", "90") + ", ".join(mapping["unknown_keys"]))
    ok = sum(1 for r in rows if r["verdict"] == "ok")
    if ok and not show_all:
        print(c(f"\n{ok} issues in sync hidden (use --all to show them).", "90"))
    moves: Dict[str, int] = {}
    for r in rows:
        if r["verdict"] == "move":
            t = target_of(r["issue"], cfg)
            moves[t] = moves.get(t, 0) + 1
    if moves:
        if len(moves) == 1:
            where = f"{sum(moves.values())} issue(s) to {next(iter(moves))}"
        else:
            where = ", ".join(f"{n} to {t}" for t, n in sorted(moves.items()))
        print(c(f"\n→ run `jgh sync` to move {where} (asks first).", "33"))


# ------------------------------------------------------------------- transition


def plan_hops(current: str, cfg: dict, target: Optional[str] = None) -> List[str]:
    path, target = cfg["workflow_path"], target or cfg["target_status"]
    if current in path and target in path:
        return path[path.index(current) + 1 : path.index(target) + 1]
    return [target]


def move_to_target(jira: "Jira", key: str, cfg: dict, dry_run: bool, current: str,
                   target: Optional[str] = None) -> bool:
    target = target or cfg["target_status"]
    for _ in range(len(cfg["workflow_path"]) + 1):
        if current == target:
            return True
        available = {t["to"]["name"]: t for t in jira.transitions(key)}
        if target in available:
            step = available[target]
        else:
            step = next((available[s] for s in plan_hops(current, cfg, target) if s in available), None)
        if step is None:
            print(c(f"    no transition from '{current}' towards '{target}' "
                    f"(available: {', '.join(available) or '-'}); issue left in '{current}'", "31"))
            return False
        if dry_run:
            # Only the first hop can be checked against Jira; later hops are the planned path.
            hops = [step["to"]["name"]]
            if hops[0] != target:
                hops += plan_hops(hops[0], cfg, target)
            print(f"    {' → '.join([current] + hops)}" + c("  (dry-run)", "90"))
            return True
        print(f"    {current} → {step['to']['name']}")
        try:
            jira.transition(key, step["id"])
        except RuntimeError as e:
            print(c(f"    transition failed: {e}\n    issue left in '{current}'", "31"))
            return False
        current = step["to"]["name"]
    return current == target


def ask(prompt: str) -> str:
    if not sys.stdin.isatty():
        return "n"
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return "n"


# --------------------------------------------------------------------- commands


def cmd_status(args, cfg, ov):
    jira = Jira(cfg)
    mapping = build_mapping(cfg, jira, ov, args.refresh)
    save_state(mapping)
    if args.json:
        print(json.dumps({"rows": mapping["rows"], "unmapped": mapping["unmapped"],
                          "unknown_keys": mapping["unknown_keys"]}, indent=2))
        return
    print_report(mapping, cfg, args.all, args.urls or cfg.get("plain_urls", False))


def cmd_sync(args, cfg, ov):
    jira = Jira(cfg)
    mapping = build_mapping(cfg, jira, ov, args.refresh)
    save_state(mapping)
    movable = ("move", "nocode") if args.keys else ("move",)
    todo = sorted((r for r in mapping["rows"] if r["verdict"] in movable), key=lambda r: r["issue"]["key"])
    if args.keys:
        wanted = {k.upper() for k in args.keys}
        todo = [r for r in todo if r["issue"]["key"] in wanted]
        for k in sorted(wanted - {r["issue"]["key"] for r in todo}):
            row = next((r for r in mapping["rows"] if r["issue"]["key"] == k), None)
            why = f"status '{row['issue']['status']}', verdict '{row['verdict']}'" if row else "not tracked"
            print(c(f"{k}: cannot be moved ({why}).", "33"))
    if not todo:
        print(c("Nothing to move.", "32"))
        return
    if not args.dry_run and not args.yes and not sys.stdin.isatty():
        print(c("stdin is not interactive: no transition performed (use --yes to force).", "33"))
        args.dry_run = True
    all_yes = args.yes
    moved = 0
    for r in todo:
        i = r["issue"]
        target = target_of(i, cfg)
        print(f"\n{c(i['key'], '1')} [{i['status']}] {trunc(i['summary'], 70)}")
        print("    PRs: " + (", ".join(fmt_pr(p) for p in r["prs"]) or c("no code needed", "90")))
        if not (all_yes or args.dry_run):
            a = ask(f"    Move to {target}? [y/N/a=all/q] ")
            if a == "q":
                break
            if a == "a":
                all_yes = True
            elif a not in ("y", "yes"):
                print(c("    skipped", "90"))
                continue
        try:
            if move_to_target(jira, i["key"], cfg, args.dry_run, i["status"], target):
                moved += 0 if args.dry_run else 1
                if not args.dry_run:
                    print(c(f"    ✓ now in {target}", "32"))
        except RuntimeError as e:
            print(c(f"    error: {e}", "31"))
    if not args.dry_run:
        print(c(f"\n{moved} issue(s) moved.", "32"))


def cmd_show(args, cfg, ov):
    jira = Jira(cfg)
    key = args.key.upper()
    mapping = build_mapping(cfg, jira, ov)
    row = next((r for r in mapping["rows"] if r["issue"]["key"] == key), None)
    if row is None:
        raw = jira.bulk_fetch([key], ISSUE_FIELDS)
        if not raw:
            die(f"{key} not found in Jira.")
        issue = simplify_issue(raw[0], jira.base)
        annotate(issue, cfg, ov)
        prs = mapping["prs_by_key"].get(key, [])
        row = {"issue": issue, "prs": prs, "verdict": classify(issue, prs, cfg)}
    i = row["issue"]
    print(f"{c(i['key'], '1')}  {i['type']}  [{i['status']}]  target: {target_of(i, cfg)}  → {row['verdict']}")
    print(f"  {i['summary']}\n  {i['url']}")
    for p in row["prs"]:
        merged = f" merged {p['merged_at'][:10]}" if p["merged_at"] else ""
        print(f"  - {fmt_pr(p)}  {p['branch']} → {p['base']}{merged}\n    {p['url']}")
    if not row["prs"]:
        print(c("  no linked PR", "90"))


def _norm_ref(ref: str) -> str:
    m = re.match(r"^https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)", ref)
    return f"{m.group(1)}#{m.group(2)}" if m else ref


def cmd_link(args, cfg, ov):
    key, ref = args.key.upper(), _norm_ref(args.pr)
    if not _REF_RE.match(ref):
        die("PR format: owner/repo#123 or the PR URL")
    ov["links"].setdefault(key, [])
    if ref not in ov["links"][key]:
        ov["links"][key].append(ref)
    if ref in ov["unlinks"].get(key, []):
        ov["unlinks"][key].remove(ref)
    _save_json(OVERRIDES_FILE, ov)
    print(f"{ref} linked to {key}")


def cmd_unlink(args, cfg, ov):
    key, ref = args.key.upper(), _norm_ref(args.pr)
    if ref in ov["links"].get(key, []):
        ov["links"][key].remove(ref)
    else:
        ov["unlinks"].setdefault(key, [])
        if ref not in ov["unlinks"][key]:
            ov["unlinks"][key].append(ref)
    _save_json(OVERRIDES_FILE, ov)
    print(f"{ref} unlinked from {key}")


def cmd_nocode(args, cfg, ov):
    key = args.key.upper()
    ov.setdefault("no_code", [])
    if args.undo:
        ov["no_code"] = [k for k in ov["no_code"] if k != key]
        print(f"{key}: expects PRs again")
    else:
        if key not in ov["no_code"]:
            ov["no_code"].append(key)
        print(f"{key}: marked as no code needed")
    _save_json(OVERRIDES_FILE, ov)


def cmd_target(args, cfg, ov):
    key = args.key.upper()
    ov.setdefault("targets", {})
    if args.undo or not args.status:
        if not args.undo:
            die("give a status, e.g. `jgh target PROJ-123 Done`, or --undo")
        ov["targets"].pop(key, None)
        print(f"{key}: back to the default target")
    else:
        ov["targets"][key] = args.status
        print(f"{key}: target status is now '{args.status}'")
    _save_json(OVERRIDES_FILE, ov)


def cmd_ignore(args, cfg, ov):
    item = _norm_ref(args.item)
    item = item.upper() if re.match(r"^[A-Za-z][A-Za-z0-9]*-\d+$", item) else item
    if args.undo:
        ov["ignore"] = [x for x in ov["ignore"] if x != item]
        print(f"{item} no longer ignored")
    elif item not in ov["ignore"]:
        ov["ignore"].append(item)
        print(f"{item} ignored")
    _save_json(OVERRIDES_FILE, ov)


def cmd_doctor(args, cfg, ov):
    missing = "" if os.path.exists(CONFIG_FILE) else c("  (missing, using defaults)", "33")
    print(f"config:    {CONFIG_FILE}{missing}")
    print(f"overrides: {OVERRIDES_FILE}")
    login = gh_graphql("query { viewer { login } }")["viewer"]["login"]
    print(f"github:    {c(login, '32')}")
    me = Jira(cfg).myself()
    print(f"jira:      {c(me.get('displayName', '?'), '32')} @ {cfg['jira_base_url']}")


def cmd_init(args, cfg, ov):
    if os.path.exists(CONFIG_FILE) and not args.force:
        die(f"{CONFIG_FILE} already exists (use --force to overwrite it).")
    new = dict(DEFAULT_CONFIG)
    new["jira_base_url"] = (args.jira_url or "").rstrip("/")
    new["jira_email"] = args.email or ""
    new["project_keys"] = [p.upper() for p in args.project or []]
    new["github_owners"] = args.owner or []
    _save_json(CONFIG_FILE, new)
    if not os.path.exists(OVERRIDES_FILE):
        _save_json(OVERRIDES_FILE, DEFAULT_OVERRIDES)
    print(f"wrote {CONFIG_FILE}")
    if not new["jira_base_url"] or not new["jira_email"]:
        print(c("set jira_base_url and jira_email there before running jgh.", "33"))
    print("check the status names (dev_statuses, workflow_path, target_status) against your Jira workflow.")


def main(argv=None):
    p = argparse.ArgumentParser(prog="jgh", description="Keep Jira issues and GitHub PRs in sync.")
    p.add_argument("--version", action="version", version=f"jgh {__version__}")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("status", help="show the issue ↔ PR mapping (default)")
    s.add_argument("--all", action="store_true", help="also show issues that are in sync")
    s.add_argument("--json", action="store_true", help="JSON output")
    s.add_argument("--urls", action="store_true", help="also print Jira URLs in plain text")
    s.add_argument("--refresh", action="store_true", help="bypass the Jira status cache")

    s = sub.add_parser("sync", help="move issues whose PRs are all merged to their target status")
    s.add_argument("keys", nargs="*", help="only these issue keys")
    s.add_argument("--dry-run", "-n", action="store_true", help="show transitions without running them")
    s.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    s.add_argument("--refresh", action="store_true", help="bypass the Jira status cache")

    s = sub.add_parser("show", help="details of one issue")
    s.add_argument("key")

    s = sub.add_parser("link", help="manually link a PR to an issue")
    s.add_argument("key")
    s.add_argument("pr", help="owner/repo#123 or PR URL")

    s = sub.add_parser("unlink", help="unlink a PR from an issue")
    s.add_argument("key")
    s.add_argument("pr", help="owner/repo#123 or PR URL")

    s = sub.add_parser("ignore", help="hide an issue or a PR")
    s.add_argument("item", help="PROJ-123 or owner/repo#123")
    s.add_argument("--undo", action="store_true")

    s = sub.add_parser("nocode", help="mark an issue as needing no code (no PR expected)")
    s.add_argument("key")
    s.add_argument("--undo", action="store_true")

    s = sub.add_parser("target", help="set the target status of one issue (e.g. Done)")
    s.add_argument("key")
    s.add_argument("status", nargs="?", help="target status name, as in Jira")
    s.add_argument("--undo", action="store_true", help="back to the default target")

    sub.add_parser("doctor", help="check configuration and authentication")

    s = sub.add_parser("init", help="create the configuration file")
    s.add_argument("--jira-url", help="e.g. https://your-company.atlassian.net")
    s.add_argument("--email", help="Jira account email")
    s.add_argument("--project", action="append", help="Jira project key (repeatable)")
    s.add_argument("--owner", action="append", help="GitHub org/user to search PRs in (repeatable)")
    s.add_argument("--force", action="store_true")

    args = p.parse_args(argv)
    if args.cmd is None:
        args = p.parse_args(["status"] + (argv if argv is not None else sys.argv[1:]))
    cfg, ov = load_config(), load_overrides()
    handlers = {
        "status": cmd_status, "sync": cmd_sync, "show": cmd_show,
        "link": cmd_link, "unlink": cmd_unlink, "ignore": cmd_ignore,
        "nocode": cmd_nocode, "target": cmd_target, "doctor": cmd_doctor, "init": cmd_init,
    }
    try:
        handlers[args.cmd](args, cfg, ov)
    except KeyboardInterrupt:
        print()
        sys.exit(130)
    except RuntimeError as e:
        die(str(e))


if __name__ == "__main__":
    main()
