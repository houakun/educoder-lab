# EduCoder API reference

Everything here was verified against the live platform. It documents what
`scripts/educoder.py` depends on, so a future change can be checked against
reality instead of guesswork.

## Origin and auth

```text
BASE = https://data.educoder.net/api
```

Every request carries a signed header set:

| Header | Value |
| --- | --- |
| `X-EDU-Type` | `pc` |
| `X-EDU-Timestamp` | current epoch **milliseconds** |
| `X-EDU-Signature` | `md5(base64("method=<METHOD>&ak=<AK>&sk=<SK>&time=<ts>"))` |
| `Pc-Authorization` | the `_educoder_session` cookie value |
| `Cookie` | `autologin_trustie=<v>; _educoder_session=<v>` |
| `Accept` | `application/json` |

`AK` / `SK` are the platform's public client constants baked into its web bundle.
A bad signature returns `{"status": -102}`.

Login is the exception: `Pc-Authorization: null`, no cookies, and it additionally
needs `Origin: https://www.educoder.net` and
`Referer: https://www.educoder.net/login`.

## Endpoints

| Purpose | Request |
| --- | --- |
| Login | `POST /accounts/login.json` `{login, password, autologin:true, tl:null, source:null}` |
| My courses | `GET /courses.json?page=1&limit=100&order=mine&search=&zzud=<zzud>` |
| Homeworks in course | `GET /courses/{cid}/homework_commons.json?limit=100&status=0&id={cid}&type=4&order=0&zzud=` |
| Entry challenge | `GET /shixuns/{shixun_identifier}/shixun_exec.json?homework_common_id={hw}&zzud=` |
| Challenge detail | `GET /tasks/{game_identifier}.json?homework_common_id={hw}&zzud=` |
| Read a code file | `GET /tasks/{gid}/rep_content.json?path={path}&homework_common_id={hw}&exercise_id=&zzud=` |
| Save code | `POST /myshixuns/{myshixun_identifier}/update_file.json?zzud=` |
| Trigger grading | `POST /tasks/{gid}/game_build.json?zzud=` |
| Submit choice answers | `POST /tasks/{gid}/choose_build.json?zzud=` |
| Challenge list (description only) | `GET /shixuns/{sid}/challenges.json?homework_common_id={hw}` |

`/tasks/{gid}.json` works **without** `homework_common_id` for standalone
practice tasks (those have `homework_common_id: null`).

### Write payloads

Save code:

```json
{
  "path": "src/step1/Singleton.java",
  "evaluate": 1,
  "content": "<full file text>",
  "game_id": 238596610,
  "tab_type": 1,
  "homework_common_id": 4197536,
  "extras": {
    "challenge_id": 5539058,
    "homework_common_id": 4197536,
    "currentUserId": 2264511,
    "exercise_id": "", "question_id": "", "subject_id": "",
    "competition_entry_id": ""
  }
}
```

Returns `content.commitID` — pass it on as `extras.commitID`.

`evaluate` is `0` for a save that should not grade. The CLI always saves with
`evaluate: 0` and triggers grading itself, once per challenge, because a
challenge is graded as a whole: a save-triggered run per file would grade a
half-applied workspace (verified: 12 file saves raised `game.evaluate_count` by
1, not 12).

Trigger grading:

```json
{
  "sec_key": "",
  "resubmit": "",
  "first": 1,
  "content_modified": 0,
  "shixun_environment_id": 1732557,
  "tab_type": 1,
  "extras": { "...": "same as above plus commitID" }
}
```

`sec_key` is frequently `null` upstream; send `""`.

`myshixun.commit_id` in `/tasks/{gid}.json` is the current commit id, so a
retry does **not** need a re-upload: POST `game_build.json` with that value.
That is what `educoder.py evaluate` does. (Verified: grading re-ran and returned
`res.code: 0`, `game.evaluate_count` +1, no file write.)

Submit choice answers:

```json
{
  "answer": ["CD", "BC", "A"],
  "challenge_id": 187694,
  "subject_id": "qlpvrn3u",
  "question_id": null,
  "competition_entry_id": null,
  "smart_plan_page_item_bank_id": null
}
```

`answer` is ordered by `test_sets[].position`, and each element is the raw
`standard_answer` string.

## Field traps

- `challenge.task_pass` holds the **statement** (Markdown/HTML), not a status.
- `challenge.path` may list several files joined by a **full-width `；`**.
- `challenge.position` / `homework_challenge_index` are 1-based.
- `game.status == 2` means finished; `game.final_score` / `game.accuracy` are the
  verdict. `game.evaluate_count` counts grading runs.
- `homework.finished_challenge_count` **lags** the real state — trust
  `game.status` per challenge instead.
- `entry_game_id` (`shixun_exec.json`) returns the **last visited** challenge.
  Use `prev_game` to rewind to the first one before walking forward.
- `test_sets[].result` is `null` **while grading is still running**, so the mere
  presence of `test_sets` is not a verdict. `last_compile_output` carries
  `compile successfully` or the compiler error.
- A verdict can describe the **previous** commit: `last_compile_output` may quote
  content already replaced (e.g. `illegal character: '\u2014'` after filling a
  blank). Re-run `game_build.json` instead of re-uploading — the run grades
  `myshixun.commit_id`, which is already current.
- Platform blanks are runs of `—` (U+2014); they are not legal Java, so a file
  still holding one can never compile.

## Response codes worth handling

| Signal | Meaning |
| --- | --- |
| `status: -102` | signature rejected (AK/SK or clock skew) |
| `status: -3` | wrong username/password |
| `status: -4` | account needs a bound phone or email |
| HTTP 401 | cached session expired — log in again |
| HTTP 429 | rate limited; back off hard |
| `status: 403` on `homework_commons/{hw}/challenges.json` | no permission for that route |

## Notes on the challenge walk

`shixun_exec.json` answers with a single `game_identifier` and ignores
`index` / `position` parameters, so there is no "give me challenge N" call. The
only way to enumerate a homework is `prev_game` / `next_game` traversal, which is
why `walk_challenges()` rewinds first and then walks forward, caching details
from the rewind so each challenge is fetched once.

A challenge's `next_game` is `null` until the previous one passes, so an
unfinished homework cannot be jumped through directly.
