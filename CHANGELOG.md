# Changelog

All notable changes to this skill are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] — 2026-10-12

### Added

- **Usage counters.** Every CLI run is counted in `~/.educoder/usage.json` — total
  runs, per-command totals, failures, first/last use, and a 30-day daily breakdown —
  and the new `stats` command reports them. Counting is local only: nothing is
  uploaded, and no account, path or argument is stored. `EDUCODER_NO_USAGE=1`
  switches it off.
- `stats --sessions` counts real agent-side usage from local session logs
  (`~/.pi/agent/sessions/**/*.jsonl`, override with `--sessions-dir`): injected
  `<skill name="educoder-lab">` blocks (loads), tool calls touching `educoder.py`
  or `SKILL.md`, and explicit `/skill:educoder-lab` commands. Mentions inside
  system prompts are deliberately ignored, so the number measures use rather than
  installation.
- `stats --json` prints the raw counters, and `SCRIPT_VERSION` / `SKILL_NAME`
  constants keep the counter file in step with the published version.

### Fixed

- A corrupt or truncated `~/.educoder/usage.json` is ignored instead of fatal, and
  the next run rewrites it (`_read_usage_file`, `_as_int`). A statistics write can
  never take the real work down: failures are swallowed (visible only under
  `EDUCODER_DEBUG=1`).

## [1.0.0] — 2026-10-09

First public release. Published under the [Agent Skills](https://agentskills.io/specification)
layout (`SKILL.md` + `scripts/` + `references/`), with a Pi package manifest and a
Claude Code plugin marketplace entry.

### Added

- `evaluate` command — re-runs grading for the current commit without re-uploading
  any file (`game_build.json` graded directly against `myshixun.commit_id`).
- `submit --dir <local-dir>` — matches local files to a challenge's remote paths
  (path suffix first, then unique basename), saves all of them, then grades once.
- `submit --force` — allows uploading content that still holds a platform blank
  (`————`, i.e. runs of U+2014), which is otherwise refused locally with zero
  requests because such a file can never compile.
- `auto --next` — prints only the next unfinished game id (or `-`), for cheap
  finishing loops.
- `_verdict_key` — `--wait` now reports a verdict only once it is settled
  (`status == 2`, or two consecutive polls agreeing). A run briefly exposes fresh
  `test_sets` while `game` still holds the pre-run status and score, which used to
  surface as a bogus `OK status=0 score=0`.

### Changed

- Saving no longer grades. `update_file.json` is always called with `evaluate: 0`
  and exactly one `game_build.json` run is triggered per challenge, so a 12-file
  challenge costs one evaluation instead of twelve (verified: `game.evaluate_count`
  rose by 1). `--no-evaluate` skips the run entirely.
- `--wait` polls a slim view (`game`/`test_sets`/`last_compile_output`) instead of
  the full challenge payload.
- Shared write path extracted into `resolve_target`, `build_extras`, `save_file`,
  `trigger_evaluate`, `wait_for_verdict`; `save_file` now reports which remote path
  it wrote.
- Windows console encoding guard no longer relies on `TextIO.reconfigure` being
  present in type stubs; `_task_view` accepts `int | None` for the homework id.

### Fixed

- A stale verdict (`last_compile_output` quoting content that was already replaced)
  is now attributed to platform lag and retried in place, instead of being reported
  as a code failure.
- Typo in the submit validation message (`拒绍` → `拒绝`).
