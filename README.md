# jgh — Jira ↔ GitHub

A small command-line tool that keeps your Jira issues in step with your GitHub pull requests.

Your Jira issues usually need to move forward (for example `In Progress → Testing`) once
the branch for that issue is merged. Checking that by hand gets tedious. `jgh` links each issue
assigned to you to the PRs whose branch name carries its key, then tells you:

- which issues are **ready to move** because all of their PRs are merged into the default branch,
- which issues were **already handed over but still have open PRs**, or no PR at all,
- which of your PRs have **no issue key** in their branch name.

It can also run the Jira transitions for you, asking first.

```
$ jgh

Ready for Testing (all PRs merged) (1)
  PROJ-142   In Progress      Retry failed webhooks with exponential backoff
                              └ api#318 ✓

Already in Testing or later, but PRs still open (1)
  PROJ-128   Testing          Paginate the audit log endpoint
                              └ api#301 ✓, api#305 draft

In progress (1)
  PROJ-150   In Progress      Add rate limiting to the public API
                              └ api#322 open

7 issues in sync hidden (use --all to show them).

→ run `jgh sync` to move 1 issue(s) to Testing (asks first).
```

Issue keys and PR refs are clickable links in terminals that support OSC 8 hyperlinks, such as
iTerm2, Ghostty, WezTerm, kitty and recent GNOME Terminal versions.

## Requirements

- Python ≥ 3.9 (standard library only, no `pip install` needed)
- The [GitHub CLI](https://cli.github.com/) logged in (`gh auth login`), or a token in `GH_TOKEN`
- A Jira Cloud API token: create one at <https://id.atlassian.com/manage-profile/security/api-tokens>

## Install

```sh
git clone https://github.com/formich/jgh.git
ln -s "$PWD/jgh/jgh.py" ~/.local/bin/jgh     # any directory on your PATH

export JIRA_API_TOKEN=...                     # e.g. in ~/.zshenv
jgh init --jira-url https://your-company.atlassian.net --email you@example.com \
         --project PROJ --owner your-org
jgh doctor                                    # checks GitHub and Jira access
```

Then open `~/.config/jgh/config.json` and **check that the status names match your Jira
workflow** (see [Configuration](#configuration)).

## Usage

```
jgh                      # report (same as `jgh status`)
jgh status --all         # include issues that are in sync
jgh status --urls        # also print Jira URLs in plain text
jgh status --json        # machine-readable output
jgh status --refresh     # bypass the Jira status cache

jgh sync -n              # dry-run: show which transitions would run
jgh sync                 # ask for each ready issue: y / N / a (all) / q
jgh sync PROJ-123        # only this issue
jgh sync -y              # no questions asked

jgh show PROJ-123        # issue details and its PRs

jgh link   PROJ-123 my-org/my-repo#42   # attach a PR manually (a PR URL works too)
jgh unlink PROJ-123 my-org/my-repo#42   # detach a PR the branch name matched
jgh ignore PROJ-123 | my-org/my-repo#42 [--undo]   # hide an issue or a PR
jgh nocode PROJ-123 [--undo]            # issue needs no code (see below)
```

## How it works

1. **PRs.** It searches GitHub for every PR you authored that was updated in the last
   `pr_lookback_days` days, plus every PR of yours that is still open, however old.
2. **Keys.** It takes the issue key from the branch name, for example `PROJ-123/fix-login`,
   `PROJ-123-fix-login` or `feature/PROJ-123_fix-login`. Placeholder keys such as `PROJ-0` are
   ignored.
3. **Issues.** It loads your issues that are in a development status, or that reached the target
   status within the lookback window. It also loads any other issue a branch points to.
4. **Verdict.** It compares each issue's status with its PRs:

| Issue status | PRs | Verdict |
|---|---|---|
| dev status (`dev_statuses`) | all merged into the default branch | **ready**: proposed by `jgh sync` |
| dev status | any open, or none | in progress |
| handed over (`handed_over_statuses`) | any still open | **warning** |
| target status | none found | **warning** |
| handed over | all merged | in sync |

- A PR counts as merged only if it was merged into the repository's **default branch**, so
  merges into `main` or `master` count and merges into other branches do not.
- PRs that were closed without merging are ignored.
- An issue with several PRs, for example a fix and a follow-up, is ready only when all of them
  are merged.

### Transitions

`jgh sync` moves an issue along `workflow_path` until it reaches `target_status`. For example,
from `In Progress` it goes to `In Review` and then to `Testing`. At each step it uses the
transitions Jira actually offers, so no transition IDs are hard-coded. If a step is not allowed,
the issue stays where it is and `jgh` says so.

The tool never transitions anything without confirmation, unless you pass `--yes`. When stdin
is not a terminal, `sync` behaves as a dry run.

### Issues that need no code

Some issues are done without a PR, for example investigations, meetings or configuration
changes. You can mark them in two ways:

- `jgh nocode PROJ-123`, which is stored locally, or
- a Jira label listed in `no_code_labels` (default `no-code`).

A marked issue never triggers the "no PR found" warning. While it is in a dev status, it shows
up in its own section, and you move it explicitly with `jgh sync PROJ-123` when you are done. If
it gets a PR after all, the normal rules apply again.

## Configuration

`~/.config/jgh/config.json` (override the directory with `JGH_HOME`):

| Key | Default | Meaning |
|---|---|---|
| `jira_base_url` | — | Your Jira site, e.g. `https://your-company.atlassian.net` |
| `jira_email` | — | Jira account email (or env `JIRA_EMAIL`) |
| `jira_api_base` | `jira_base_url` | API base; for scoped tokens use `https://api.atlassian.com/ex/jira/<cloudId>` |
| `project_keys` | `[]` | Projects to track; empty means any `ABC-123` key in branch names |
| `github_owners` | `[]` | Orgs/users to search PRs in; empty means all PRs you authored |
| `pr_lookback_days` | `90` | How far back to look for merged PRs |
| `dev_statuses` | `To Do`, `In Progress`, `In Review` | Statuses still owned by the developer |
| `workflow_path` | `To Do` → `In Progress` → `In Review` → `Testing` | Path walked by `sync` |
| `target_status` | `Testing` | Hand-over status |
| `handed_over_statuses` | `Testing`, `Done` | Statuses at or past the hand-over |
| `terminal_statuses` | `Done` | Statuses that are safe to cache |
| `issue_cache_hours` | `72` | Cache lifetime for terminal issues |
| `no_code_labels` | `no-code` | Jira labels meaning "no code needed" |
| `plain_urls` | `false` | Always print Jira URLs in plain text |
| `placeholder_key_regex` | `^[A-Z][A-Z0-9]+-0+$` | Branch keys to ignore |

Environment variables: `JIRA_API_TOKEN` (required), `JIRA_EMAIL`, `JIRA_BASE_URL` and
`JIRA_API_BASE` (optional overrides), `GH_TOKEN` or `GITHUB_TOKEN` (optional; otherwise
`gh auth token` is used), and `NO_COLOR`.

Other files in the same directory:

- `overrides.json` holds your manual `link`, `unlink`, `ignore` and `nocode` entries.
- `state.json` holds the last mapping snapshot and the issue status cache.

Tokens are only read from the environment and are never written to disk.

## Performance

A typical run takes about as long as a single GitHub search, roughly 1–1.5 s, for these reasons:

- GitHub is queried over GraphQL directly, and the PR searches run in parallel, each on its
  own keep-alive connection.
- The Jira query runs at the same time as the GitHub searches, over a keep-alive connection.
- Issues that are already done are served from a short-lived cache instead of being
  re-fetched.

## Development

```sh
python3 -m unittest discover -s tests
```

## License

[MIT](LICENSE)
