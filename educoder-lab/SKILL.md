---
name: educoder-lab
description: Completes 头歌(EduCoder) classroom labs end to end through the platform JSON API, with no browser and no page snapshots. Lists courses/homeworks, reads a challenge's statement and code, auto-submits multiple-choice challenges (the platform exposes its own answers), submits code challenges for grading, and walks every challenge of a homework. Use when the user asks to finish EduCoder/头歌 课堂实验/作业/实训/关卡, or to find what is still unfinished in their account.
license: MIT
compatibility: Requires Python 3.10+ and network access to data.educoder.net, plus an EduCoder account. No third-party packages and no browser.
metadata:
  author: houakun
  version: "1.0.0"
  repository: https://github.com/houakun/educoder-lab
  language: python
---

# 头歌 (EduCoder) lab automation

Drive `scripts/educoder.py` — Python stdlib only, no dependencies, no browser.
It talks to `https://data.educoder.net/api` directly, so a challenge takes a few
JSON calls instead of page snapshots. Run commands from the skill's `scripts/`
directory.

## Requirements

- Python 3.10+ (uses `X | None` annotations). No third-party packages.
- An EduCoder account. Never paste the password into chat — put it in
  `EDUCODER_PASSWORD` or let `login` prompt for it.

## Start here

```bash
python educoder.py login --login <account>      # prompts for password, caches session
python educoder.py status                       # confirm the cached session still works
python educoder.py courses                      # find the course identifier
python educoder.py homeworks --course <cid>     # TODO rows are unfinished; gid= is the entry challenge
```

## The two challenge kinds

**Multiple choice** — the platform returns its own answers, so there is nothing
to solve:

```bash
python educoder.py task --game <gid> --homework-id <hw>     # prints answers=[...]
python educoder.py choose --game <gid> --subject-id <sid>   # submits them
```

**Code** — read, edit locally, submit:

```bash
python educoder.py task --with-code --game <gid> --homework-id <hw>
python educoder.py submit --game <gid> --homework-id <hw> \
    --path <remote/file.java> --file ./work/file.java --dry-run
python educoder.py submit --game <gid> --homework-id <hw> \
    --path <remote/file.java> --file ./work/file.java --wait
```

Multi-file challenges (the `files:` line lists several) are graded as a whole,
so upload them in one command — `--dir` matches local files to remote paths,
saves every one of them, and triggers a single grading run:

```bash
python educoder.py submit --game <gid> --homework-id <hw> --dir ./work --wait
```

Retry a stale verdict without re-uploading anything:

```bash
python educoder.py evaluate --game <gid> --homework-id <hw> --wait
```

One-shot walk of a whole homework (auto-submits choices, lists code left):

```bash
python educoder.py auto  --course <cid> --homework <hw>        # read-only survey
python educoder.py auto  --course <cid> --homework <hw> --next # one line: next unfinished gid
python educoder.py solve --course <cid> --homework <hw>        # submits choice challenges
```

## Usage rules

- Pass `--game <identifier>` and `--homework-id <hw>` whenever they are known —
  it skips the course/homework resolution round trips.
- `homeworks` prints `gid=` for TODO rows; use that value as `--game`.
- For multi-file challenges take the `files:` line from `task`, then either
  pass one exact remote path per `submit`, or point `--dir` at a local folder
  that holds those files (matched by path suffix, then by unique basename).
- Finishing loop: `homeworks` (cached) → next TODO `gid=` → `task --with-code` →
  edit → `submit --dir --wait` → repeat. `auto --next` prints just that next
  gid (`-` when the homework is done) when you do not want the full survey.
- The remote path includes its directory: `src/step1/Singleton.java`, not
  `Singleton.java`.
- `--dry-run` first when the target is the least bit ambiguous, then drop it.
- Iterate from the printed divergence instead of re-reading the whole payload.
- `task --with-code` replaces `task` + `code`: one call instead of two.
- Status checks stay tiny: `task --game <gid> --json --fields game` — a bare
  `--json` re-prints the whole statement and costs thousands of tokens.

## Verified examples

```bash
# choice challenge — answers come from the platform, not from reasoning
python educoder.py task --game ea7siqp6ugzj --homework-id 4197536
# -> answers=["CD", "BC", "A"]
python educoder.py choose --game ea7siqp6ugzj --subject-id qlpvrn3u
# -> answers=["CD", "BC", "A"] correct=3/3 gold=30

# code challenge — single submit, compressed verdict
python educoder.py submit --game vhei6q259mxc --homework-id 4197536 \
    --path "src/step1/Singleton.java" --file ./Singleton.java --wait
# -> save: path=src/step1/Singleton.java commit=83fd292e8c19
# -> evaluate: code=0 msg=评测完成
# -> OK status=2 score=100 accuracy=1.0

# multi-file challenge — 12 files, exactly one grading run
python educoder.py submit --game 3aswtxcerpyg --homework-id 4197533 --dir ./step2 --wait
# -> save: path=src/step2/Airsensor.java commit=964f02a193da   (x12)
# -> evaluate: code=0 msg=评测完成
# -> OK status=2 score=100 accuracy=1.0
# game.evaluate_count went 1 -> 2: the saves did not grade on their own

# stale verdict — re-grade the current commit, no upload
python educoder.py evaluate --game 3aswtxcerpyg --homework-id 4197533 --wait
# -> evaluate: code=0 msg=评测完成
# -> OK status=2 score=100 accuracy=1.0

# blank left in the file — refused locally, zero requests
python educoder.py submit --game 3aswtxcerpyg --homework-id 4197533 --dir ./step2 --dry-run
# -> error: 拒绝提交 MeshAir.java（src/step2/MeshAir.java）：内容仍是平台占位符
#    [line 6: return ——————;]，上传后必然编译失败。补全后再提交，确需原样上传时加 --force

# where do I go next?
python educoder.py auto --course 125505 --homework 4197533 --next
# -> 7qf5ay9htsj8   (or - when nothing is left)
```

## Gotchas

- **The statement lives in `challenge.task_pass`**, not `challenge.description`.
  Despite the name it is not a pass/fail flag.
- **A challenge is done when `game.status == 2`** (or every `test_sets[].result`
  is non-null). Never infer it from `task_pass`, and never from
  `homework.finished_challenge_count` — that counter lags (a challenge set that
  was fully `status=2` still reported 3/4).
- **`myshixun_identifier` is not a challenge id.** The `--game` value comes from
  `/shixuns/{shixun_identifier}/shixun_exec.json?homework_common_id=<hw>`.
- **That endpoint returns the *last visited* challenge, not the first
  unfinished one.** A partially finished homework can hand back an already
  finished challenge, so walking forward from it silently skips earlier
  unfinished work. `walk_challenges()` rewinds `prev_game` to the real first
  challenge; do not replace it with a plain `next_game` loop.
- **Multi-file challenges separate paths with a full-width semicolon `；`.**
  The `files:` line already splits them for you.
- **`sec_key` is often `null`** — send `""` rather than omitting the field.
- **A save does not grade.** `submit` writes files with `evaluate: 0` and then
  triggers exactly one `game_build.json` run per challenge. Grading per uploaded
  file would grade a half-applied workspace (a 12-file challenge would burn 12
  runs against a broken tree). `--no-evaluate` skips the run entirely.
- **A verdict can lag one upload behind.** The platform sometimes grades the
  commit *before* the one you just wrote, so the compile log quotes content you
  already replaced (e.g. `illegal character: '\u2014'` for a filled-in blank).
  That is not a code bug: re-run `evaluate` — never re-upload to retry.
- **`myshixun.commit_id` is the current commit id**, and `evaluate` grades that,
  so a retry costs one request instead of a full re-upload.
- **Platform blanks are runs of `—` (U+2014), which is not legal Java.** Uploading
  one guarantees a compile failure, so `submit` refuses it locally (0 requests);
  `--force` overrides.
- **`game_build.json` does not return the verdict.** It only starts the run
  (`msg: 评测完成`, `res.code: 0`); re-read `/tasks/{gid}.json` for
  `last_compile_output` and `test_sets`.
- Requests are signed with
  `md5(base64("method=<METHOD>&ak=..&sk=..&time=<ms>"))` plus
  `Pc-Authorization: <_educoder_session>`. The script does this; hand-rolled
  requests without it fail with `status: -102`.

## Output control

Defaults are terse; raise them only when needed.

- `--json` prints raw JSON; `--fields game,challenge,next_game` trims it
  (14091 → 4725 chars on a real challenge). For a verdict, `--fields game` is
  enough — the statement in `challenge.task_pass` dominates the payload.
- `--max-output N` caps printed text and the statement (default 2000).
- `--refresh` bypasses the course/homework cache (TTL 600s, in `~/.educoder/cache/`).
- `--wait` polls a slim view (game/test_sets/last_compile_output), so polling
  never re-prints the statement or the file list. It reports a verdict only once
  it is settled (`status == 2`, or two consecutive polls agreeing): a run briefly
  exposes fresh `test_sets` while `game` still holds the pre-run status/score,
  which reads as a bogus `OK status=0 score=0`.
- Failures print a located divergence such as
  `line 2 col 7: expected 'line2 RIGHT' got 'line2 WRONG'`, plus missing or extra
  lines — usually enough to fix the code without re-reading anything.

## Safety

- `submit` is a **remote write** and `evaluate`/`choose` **start grading**. Both
  cost evaluator quota; confirm the target first and `--dry-run` shows exactly
  what would be uploaded.
- Save the files, grade once. One grading run per challenge is normally enough:
  read the printed divergence (or the stale-verdict note above) before retrying.
- Credentials live in `~/.educoder/session.json` (mode 0600). Never echo that
  file, the cookies, or the `zzud` value into the conversation.
- This runs against the user's own account and courses. Do not point it at
  someone else's account.

## References

- `references/api.md` — endpoints, headers and field traps this script relies on.
