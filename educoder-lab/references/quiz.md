# Choice challenges (客观题) — when the API does not carry the questions

Some EduCoder choice challenges are "graded pools": the platform does **not**
send the question text, the options, or the standard answers through the JSON
API. `choose` then has nothing to submit, and `task` shows only a title.

This file documents the only working path found so far: read the questions from
the rendered page with a browser, answer them, then submit **from the page**.

## 1. Confirm it is this kind of challenge

```bash
python educoder.py task --game <gid> --homework-id <hw>
```

Signs that this file applies:

- `--- choice questions (N) ---` with `answer=None` on every row.
- `challenge_path=None`, and `task_pass` is just the title (e.g. 8 characters).
- `choose --game <gid>` refuses with *"没有可提交的选择题答案"*.

Do not keep probing API endpoints for the questions. These were tried and
return HTTP 302 (no route / no permission):

```text
/tasks/{gid}/choose_test_cases.json
/tasks/{gid}/chooses.json
/challenges/{cid}/chooses.json
```

`Mentions in <challenge>` note: after a **successful API submission** the
response does include `test_sets[].standard_answer`, which is useful for
calibration — but a page submission does not expose it again.

## 2. Open the challenge with your cached session

`~/.educoder/session.json` holds the cookies. Never print their values.

```bash
SESSION=$(python -c "import json,os;print(json.load(open(os.path.expanduser('~/.educoder/session.json')))['session'])")
AUTO=$(python -c "import json,os;print(json.load(open(os.path.expanduser('~/.educoder/session.json')))['autologin'])")
playwright-cli open
playwright-cli cookie-set _educoder_session "$SESSION" --domain=.educoder.net --secure
playwright-cli cookie-set autologin_trustie "$AUTO" --domain=.educoder.net
playwright-cli goto "https://www.educoder.net/tasks/<gid>"
sleep 6
playwright-cli eval "document.title"     # must be the challenge name, not just 头歌实践教学平台
```

The page is JavaScript-rendered: `fetch_content` returns nothing useful, and
`curl` gets an empty shell. A real browser (Playwright) is required.

## 3. Extract the questions

The whole pool lives in `UL.choose-container`. Read it in slices so the output
is not truncated:

```bash
playwright-cli eval "(() => { const t = document.querySelector('.choose-container').innerText; return JSON.stringify({len: t.length}); })()"
playwright-cli eval "(() => { const t = document.querySelector('.choose-container').innerText; return t.slice(0, 5600); })()"
playwright-cli eval "(() => { const t = document.querySelector('.choose-container').innerText; return t.slice(5500, 11100); })()"
```

Layout to expect: `N、 <question>` then `A、 <option>` … Some questions reference
a figure (`下图`) or a table (`下表`); those are separate `<img>` attachments —
see section 7.

## 4. Answer, then keep the answers in a file

Write them as a JSON array (position order) or a `{position: answer}` map. The
file is reviewable, survives shell quoting, and can be replayed after a redo:

```json
{"1": "C", "2": "C", "3": "C", "4": "C"}
```

Validate before submitting — this checks the count against the platform's
question count and never writes anything:

```bash
python educoder.py choose --game <gid> --homework-id <hw> \
    --answer-file answers.json --dry-run
# -> DRY-RUN game=... challenge=... questions=102 answers=102
```

A count mismatch is refused:

```text
error: 答案数量 41 与题目数量 42 不一致，拒绝提交
```

## 5. Submit from the page, or replay the exact payload

A resubmission is **not** always rejected. It was rejected once with:

```json
{"status": -1, "message": "题目有更新，请刷新后再次作答！"}
```

but that attempt also had **one answer too many** (43 for 42 questions), so read
that message as "the answer array did not match the pool" rather than a hard
lock. Check the count first, then retry — and if it still refuses, use the page.

The page path always works:

```bash
# 1) click one option per question (index: A=0 B=1 C=2 D=3)
playwright-cli eval "(() => { const items = document.querySelectorAll('.option'); const idx = [/* one index per question */]; let clicked = 0; const missing = []; idx.forEach((j,i) => { const it = items[i]; if (!it) { missing.push(i+1); return; } const opts = it.querySelectorAll('a.flex-container'); if (opts[j]) { opts[j].click(); clicked++; } else missing.push((i+1)+':'+opts.length); }); return JSON.stringify({items: items.length, clicked, missing}); })()"
# 2) submit
playwright-cli eval "(() => { const b = [...document.querySelectorAll('button')].find(x => x.innerText.trim() === '评测'); if (!b) return 'no button'; b.click(); return 'clicked'; })()"
```

Verify with `{items: 102, clicked: 102, missing: []}`; then poll the API:

```bash
python educoder.py task --game <gid> --homework-id <hw> --json --fields game
```

### Read the authoritative payload instead of guessing it

When the answer format is unclear, capture what the page actually sends. Hook
`fetch`/`XHR` first, then press `评测`:

```bash
playwright-cli eval "(() => { window.__caps = []; const of = window.fetch; window.fetch = function(...a) { try { if (a[1] && a[1].body) window.__caps.push({u: String(a[0]), b: String(a[1].body)}); } catch (e) {} return of.apply(this, a); }; const xo = XMLHttpRequest.prototype.send; XMLHttpRequest.prototype.send = function(b) { try { if (b) window.__caps.push({u: this.__url || '', b: String(b)}); } catch (e) {} return xo.call(this, b); }; const op = XMLHttpRequest.prototype.open; XMLHttpRequest.prototype.open = function(m, u, ...r) { this.__url = String(u); return op.call(this, m, u, ...r); }; return 'hooked'; })()"
playwright-cli eval "(() => { const b = [...document.querySelectorAll('button')].find(x => x.innerText.trim() === '评测'); b.click(); return 'clicked'; })()"
playwright-cli eval "JSON.stringify((window.__caps || []).filter(c => c.u.includes('choose_build')).map(c => c.b))"
```

That is how the two non-obvious fields below were confirmed on a live 42-question
pool: `subject_id` is `""` (not `null`), and a true/false question is answered
with the string `"true"` (not `A`, not `正确`).

## 6. Find the wrong answers after a page submission

The API stops returning per-question results once the page owns the submission,
so read the result panel instead:

```bash
playwright-cli eval "(() => { const p = document.querySelector('.test-result'); return p ? p.innerText.replace(/\\s+/g,' ') : 'none'; })()"
# -> "98 / 102 共有102题，其中4题目结果不匹配"
```

Each question is a `.test-case-item` whose `<img>` carries a pass or fail SVG.
Group by SVG fingerprint: the minority group is exactly the wrong questions.

```bash
playwright-cli eval "(() => { const items = [...document.querySelectorAll('.test-case-item')]; const g = {}; items.forEach((it,i) => { const img = it.querySelector('img'); const k = img ? img.src : ''; (g[k] = g[k] || []).push(i+1); }); return JSON.stringify(Object.entries(g).map(([k,v]) => ({count: v.length, questions: v}))); })()"
# -> [{"count":98,...},{"count":4,"questions":[17,18,99,101]}]
```

Fix those questions, submit again from the page (`评测`), and re-check
`game.status`:

```bash
python educoder.py task --game <gid> --homework-id <hw> --json --fields game
# -> status: 2, final_score: 1020  (full marks)
```

## 7. Figures and tables

```bash
playwright-cli eval "JSON.stringify([...document.querySelectorAll('.choose-container img')].map(i => i.src).filter(s => s.includes('attachments')))"
```

Download with the session cookie, then look at the image:

```bash
python - <<'PY'
import json, os, urllib.request
s = json.load(open(os.path.expanduser('~/.educoder/session.json'), encoding='utf-8'))
url = "https://data.educoder.net/api/attachments/<id>?type=image/png"
req = urllib.request.Request(url, headers={
    "Cookie": f"_educoder_session={s['session']}; autologin_trustie={s['autologin']}",
    "Pc-Authorization": s["session"], "User-Agent": "Mozilla/5.0"})
print(len(urllib.request.urlopen(req, timeout=30).read()), "bytes available")
# then save to a file and read it as an image
PY
```

Waveform questions are usually decodable from the image alone (sample the dark
pixels per column, find the bit boundaries, compare the level at each boundary);
do the arithmetic on paper rather than guessing.

## 8. Gotchas that cost points

- **True/false questions are answered with `"true"` / `"false"`.** Not `A`, not
  `B`, not `正确`. On the page they are just the first/second label
  (`正确`/`错误`); in the API payload the value is the lowercase string. Feeding
  letters there costs exactly one question per pool.
- **The answer array must match the question count exactly.** A 43-element answer
  for a 42-question pool came back as the misleading
  `题目有更新，请刷新后再次作答！`. `choose` refuses the mismatch locally now.
- **`status == 2` requires a perfect score.** One wrong answer in a 42-question
  pool gives `final_score: 400` and `status: 0`; the challenge is not finished.
  A later perfect submission does restore `status: 2`/`420` — the platform keeps
  the best result, so a corrected replay is safe.
- **Do not hardcode answers.** Pools change; answers are per attempt. Keep the
  answer file for the current attempt only.
- **Selectors are private front-end class names** (`.choose-container`,
  `a.flex-container`, `.option`, `.test-case-item`). If they stop matching,
  probe the tree first instead of guessing:

```bash
playwright-cli eval "(() => { let el = document.querySelector('.ant-radio-wrapper'); const out = []; for (let i = 0; i < 8 && el; i++) { out.push(el.tagName + '.' + el.className); el = el.parentElement; } return JSON.stringify(out); })()"
```

## 9. Completion check

```bash
python educoder.py homeworks --course <cid> --refresh | grep TODO
```

A pool is done when the homework row shows `DONE`, which for these challenges
means every question is correct (`status == 2`).
