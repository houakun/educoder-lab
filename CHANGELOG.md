# Changelog

All notable changes to this skill are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] — 2026-10-25

Driven by a real 165-question run (two networking pools) that exposed the gaps
below.

### Added

- `choose --answer-file <path>` — answers from a JSON list, a `{position: answer}`
  map, or one per line (`#` comments allowed). A file survives shell quoting and
  can be replayed; 100+ positional arguments cannot. UTF-8 with a GBK fallback,
  because Windows editors still write GBK.
- `choose --dry-run` — prints the pool size and a preview, validating the answer
  count against the platform without submitting.
- `choose` now refuses a count mismatch (`答案数量 41 与题目数量 42 不一致`) — a
  43-element array for a 42-question pool is what produced the misleading
  `题目有更新，请刷新后再次作答！`.
- `choose` prints a one-line wrong-answer digest
  (`wrong=1/42: pos=4 mine='错误' std='正确'`) after a submission.
- `references/quiz.md` — the browser path for pools the API cannot serve: cookie
  injection, question extraction, page submission, wrong-answer detection from the
  result panel, figure/table downloads, and a fetch/XHR hook that captures the
  page's real payload instead of guessing it.

### Fixed

- `choose` sends `subject_id: ""` (the page's value) instead of `null`.
- A non-UTF-8 answer file now falls back to GBK and reports a clear error, instead
  of dying with a `UnicodeDecodeError` traceback.
- An API rejection is mapped to `提交被拒绝：…` with a pointer to
  `references/quiz.md` rather than printing an empty `correct=None/None` line.

### Documented

- True/false questions are answered with the strings **`"true"` / `"false"`**;
  `A`, `B` and `正确` are all marked wrong (verified on a live pool).
- A pool is finished only at a perfect score: one miss in 42 gives
  `final_score: 400, status: 0`. A later correct submission restores `status: 2`.
- Re-submitting is not a hard lock: check the answer count before believing
  `题目有更新`.
- `courses`/`homeworks` can omit a course until `--refresh` (a whole course was
  missed on the first sweep).

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
