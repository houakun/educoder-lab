# educoder-lab

An [Agent Skill](https://agentskills.io/specification) that finishes **头歌 (EduCoder)**
classroom labs end to end — through the platform's JSON API, with no browser and no
page snapshots. A challenge costs a handful of JSON calls instead of a screen-scraping
session.

Works with [Pi](https://github.com/badlogic/pi-mono), Claude Code, and any agent that
discovers skills by a `SKILL.md` file.

## What it does

- **Lists your courses and homeworks**, and tells you which challenges are still open.
- **Reads a challenge** — statement, test cases, expected output, and the code files.
- **Multiple-choice challenges** are answered from the platform's own data: EduCoder
  returns the standard answers, so there is nothing to solve.
- **Code challenges** are read, edited locally, saved back, and graded.
- **Walks a whole homework** and reports what is left.

The CLI talks to `https://data.educoder.net/api` directly, signs its own requests, and
caches the session after one login.

## Install

### Pi

```bash
# as a Pi package (skills come from package.json -> pi.skills)
pi install git:github.com/houakun/educoder-lab

# or clone straight into a skills directory (user-level, or .pi/skills for a project)
git clone https://github.com/houakun/educoder-lab ~/.pi/agent/skills/educoder-lab
```

### Claude Code

```bash
# via the plugin marketplace entry in .claude-plugin/marketplace.json
/plugin marketplace add houakun/educoder-lab
/plugin install educoder-lab@educoder-lab

# or by cloning and linking the skill folder (Claude Code only scans one level deep)
git clone https://github.com/houakun/educoder-lab ~/educoder-lab
mkdir -p ~/.claude/skills
ln -s ~/educoder-lab/educoder-lab ~/.claude/skills/educoder-lab
```

### Other agents

Point the agent at `educoder-lab/SKILL.md`, or copy the `educoder-lab/` directory into
whatever directory it reads skills from (`.agents/skills/`, `.codex/skills/`,
`.factory/skills/`, …).

## Use

Log in once; the session is cached at `~/.educoder/session.json` (mode 0600). Never put
the password in chat — export it or let the script prompt:

```bash
export EDUCODER_PASSWORD='...'          # or answer the prompt
python educoder-lab/scripts/educoder.py login --login <account>
```

Then:

```bash
python educoder.py status                        # is the cached session still good?
python educoder.py courses                       # find the course identifier
python educoder.py homeworks --course <cid>      # TODO rows are unfinished
python educoder.py auto --course <cid> --homework <hw> --next   # next open game id
```

Read a challenge and grade the code:

```bash
python educoder.py task --with-code --game <gid> --homework-id <hw>
python educoder.py submit --game <gid> --homework-id <hw> --dir ./work --dry-run
python educoder.py submit --game <gid> --homework-id <hw> --dir ./work --wait
```

Multi-file challenges are graded as a whole, so `--dir` uploads every matching file and
triggers **one** grading run. If a verdict looks stale, re-grade without re-uploading:

```bash
python educoder.py evaluate --game <gid> --homework-id <hw> --wait
```

### Commands

| Command | Purpose |
| --- | --- |
| `login` | Authenticate and cache the session |
| `status` | Check the cached session |
| `courses` | List your courses |
| `homeworks --course <cid>` | List homeworks; `TODO` rows are unfinished |
| `task --game <gid>` | Statement, tests, files, `prev_game`/`next_game` |
| `code --game <gid> [--path <remote>]` | Read remote answer file(s) |
| `submit --game <gid> --file` / `--dir` | Save code and grade it once |
| `evaluate --game <gid>` | Re-run grading for the current commit |
| `choose --game <gid> --subject-id <sid>` | Submit a multiple-choice challenge |
| `auto --course <cid> --homework <hw> [--next]` | Survey a homework |
| `solve --course <cid> --homework <hw>` | Walk a homework, auto-submitting choices |
| `stats [--sessions]` | How often this skill was used (local counters, or session logs) |

Useful flags: `--dry-run` (show the exact upload plan), `--wait` (poll for the verdict),
`--no-evaluate` (save only), `--force` (upload even with a platform blank left),
`--json --fields game,test_sets` (tiny status output), `--max-output N`, `--refresh`.

### Usage stats

Every run is counted locally in `~/.educoder/usage.json`, and `stats` reports it:

```bash
python educoder.py stats                # runs, per-command totals, recent days
python educoder.py stats --sessions     # also count real usage in agent session logs
```

```text
local counters: ~/.educoder/usage.json
  total runs: 5   failures: 0
  first: 2026-10-12T18:45:05   last: 2026-10-12T18:45:10
    stats      3
    auto       1
    task       1
  recent days: 2026-10-12:5
agent sessions: ~/.pi/agent/sessions
  skill loads: 1   tool calls: 174   /skill: commands: 0
  sessions using it: 3/47 scanned
  first: 2026-09-24T10:54:11.250Z   last: 2026-10-12T10:45:23.559Z
note: counters are local to this machine; nothing is uploaded.
```

- **Nothing is uploaded.** No endpoint is contacted, and only the command name, a
timestamp and the outcome are stored — never an account, a path or an argument.
Set `EDUCODER_NO_USAGE=1` to switch counting off.
- `--sessions` scans `~/.pi/agent/sessions/**/*.jsonl` (override with
`--sessions-dir`) and counts three real-usage signals: an injected
`<skill name="educoder-lab">` block (the skill was loaded), a tool call touching
`educoder.py` or `SKILL.md`, and an explicit `/skill:educoder-lab` command. Mentions
inside system prompts are ignored on purpose: an installed skill is advertised in
every session, so counting those would measure installation rather than use.
- A cross-user total cannot come from the client without telemetry, which this skill
deliberately does not do. Use the repository's traffic and stars for rough popularity.

## Requirements

- **Python 3.10+** — standard library only. No third-party packages, no `pip install`.
- **Network access** to `data.educoder.net`.
- **Your own EduCoder account.** This drives your account; do not point it at somebody
  else's.

## Safety notes

- `submit` writes to the platform and `evaluate`/`choose` start real grading runs, which
  consume evaluator quota. Confirm the target first; `--dry-run` shows exactly what would
  be uploaded.
- Credentials live in `~/.educoder/session.json` (mode 0600). Do not echo that file, the
  cookies, or the `zzud` value into a conversation.
- The script refuses to upload a file that still contains a platform blank (`————`,
  runs of U+2014) because it can never compile. `--force` overrides that check.
- A verdict can describe the previous commit; that is platform lag, not a code bug —
  re-run `evaluate` instead of re-uploading.

## Repository layout

```text
educoder-lab/
├── SKILL.md                  # skill instructions (Agent Skills format)
├── scripts/educoder.py       # the whole CLI, stdlib only
├── references/api.md         # endpoints, headers, field traps
├── educoder-lab/             # copy that agents load
├── .claude-plugin/marketplace.json
├── package.json              # Pi package manifest (pi.skills)
└── CHANGELOG.md
```

## License

[MIT](LICENSE). This is an unofficial automation client for a platform you have an
account on; use it in line with EduCoder's terms.
