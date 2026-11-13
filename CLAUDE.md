# Biscuits Oracle: Project Plan

A free website that shows Disco Biscuits setlist stats and predicts the next show's setlist.

## How to work with the user (read this first)

- The user is a beginner. Explain things simply and skip jargon, or explain it when you use it.
- Give **one step at a time**. Wait for the user to finish it before moving on.
- **Run commands yourself** (Claude runs them). Only give the user a copy-paste block for things only they can do: logging into websites, interactive logins, or clicking in a browser.
- Use the project's Python at `.venv\Scripts\python.exe`. The virtual environment is already set up with `requirements.txt` installed.
- The user is on **Windows with PowerShell**. Commands must work in PowerShell, not bash.
- The user has Python and a GitHub account.
- **Never** put the API key in code, in commits, or in chat output. It lives only in `.env` on the user's computer and in a GitHub Secret.

---

## 1. Where the data comes from (researched 2026-11-13)

discobiscuits.net has **no normal REST API**. `/api` and `/api/v5` return 404, and `api.discobiscuits.net` does not exist.
It has an **MCP server**, which is a web address you send JSON messages to:

| Item | Value |
|---|---|
| Address | `https://discobiscuits.net/mcp` |
| Format | HTTP POST with JSON-RPC 2.0, for example `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_setlist","arguments":{"slug":"..."}}}` |
| API key | Create one under Profile → Settings. Send it as the header `Authorization: Bearer <key>` |
| Rate limit | **60 requests per minute** per key |
| Terms | Personal, non-commercial use only. Setlists belong to the band. No ads and no reselling the data. |

The tools we'll use:
- `get_shows_by_year(year, limit≤500, sortBy)` lists a year's shows. Each show has a `slug` like `2026-02-06-miami-beach-bandshell-miami-beach-fl`, plus date, venue, city, state, and rating.
- `get_setlist(slug)` and `get_setlists([slugs])` return the sets (`S1`, `S2`, `E1`…). Each track has `position`, `song`, `segue` (`>` or null), and `notes` (free text).
- `get_venues`, `get_songs`, and `get_song_statistics` are useful for checking our numbers.

### Step 3 findings (explored 2026-11-13 with the real key; raw samples are in `data/samples/`)

The connection uses `scripts/mcp_client.py`. It runs `initialize`, then `tools/call`, with a Bearer header and waits 1.1 s between calls. The result is JSON text inside `content[0].text`.

**Use `get_setlists` (batch), not `get_setlist`.** The batch version returns much richer data. Batches of 25 slugs work.
- Show: `showSlug`, `showDate`, `venue{name,city,state}`, `lineup[]` (musicians with instruments, which can identify eras and guests), `sets[]`.
- Set: `label` is `S1`, `S2`, `S3`, `E1`, …
- Track: `id` (UUID), `position`, `songTitle`, `songSlug`, `segue`, `flags[]`, `completes[]`, `annotations[]`, `trackMusicians[]`, `note`, `duration` (seconds), `allTimer`.
- **`segue`:** `">"` means this track segues **into the next track**. `""` and `null` both mean no segue. A `">"` on the last song of a set is rare, about 3 per 400 sets.
- **`flags`** are structured: `INVERTED`, `UNFINISHED`, `DYSLEXIC`, `MIDDLE_ONLY`, `ENDING_ONLY`. We haven't seen `BEGINNING_ONLY` yet, but the code should accept any flag value.
- **`completes`** is structured: `[{showSlug, set, position}]` points to the earlier performance this one finishes. It is often from the previous night, so completions cross shows within a run. This is the source for the `completions` table. An "open thread" is a flagged performance that no later track completes.
- **`trackMusicians`** lists sit-ins, for example `{musicianSlug, musicianName, instruments[]}`.
- **Teases and samples** are free text in `annotations`, like `with ‘Song’ (Artist) tease(s)`, `... samples`, or `... jam`. These need a parser. The quote marks are curly (`‘ ’`).
- **Tractorbeam** is marked per *track* by an annotation such as "Tractorbeam Remix" or "Tractorbeam version", not per set. Tag it at the track level. A set where every track is Tractorbeam counts as a Tractorbeam set.
- **Powered Down is NOT marked anywhere in the API.** The search for "powered down" only fuzzy-matched words like "Tower" and "Downtime". The user chose a hand-kept list with help from a scan (see section 11).

**Powered Down candidate scan (runs during the backfill):** write `data/powered_down_candidates.txt` as **plain text** for the user to check. Never add anything to the config list without the user's confirmation. Flag a show or set if any of these is true:
1. The show `notes`, a track `note`, or an `annotation` mentions "acoustic", "powered down", "unplugged", "stripped", or "seated".
2. The band name or billing differs from "The Disco Biscuits" in any field the API returns, for example a notes line like "billed as …".
3. The `lineup` differs from that era's usual core four: a member is missing, an extra non-guest musician is listed, or instruments differ (for example acoustic guitar or piano instead of keyboards).

Each line gives the date, the set (or "whole show"), and the reason it was flagged. Confirmed entries go in `config.toml` as `powered_down = ["YYYY-MM-DD S1", "YYYY-MM-DD"]`, where a date alone means the whole show.

`get_shows(slugs)` adds show-level fields: `id`, `notes` (for example the opener band or "Setlist incomplete"), `countForStats` (bool; respect it in the stats math), `dayOrder` (for two shows on one day), `relistenUrl`, and ratings.

`get_shows_by_year(year, limit=500)` lists the shows in a year. 2025 had 91 shows.

**Data quirks to handle:**
- Some year lists include **placeholder shows** with an empty venue and a bare slug like `2025-10-31`. Skip them, because `get_setlists` returns "Not found" for them.
- A slug's date can differ from the real date (`2025-07-06-cape-cod-...` has date `2025-07-09`). **Always use the `date` field, never the slug.**
- `search_by_date` with `dateType: "month"` returns wrong results. Don't use it.

**Upcoming shows are not in the API.** Instead, `scripts/next_show.py` reads the "Next Show" box on the discobiscuits.net homepage. That's one plain request with our generic User-Agent, and `robots.txt` allows `/` for normal agents. The script parses the `<h3>Next Show</h3>` box: the date comes first (`Dec 14, 2026`), then `Venue • City, ST`. If reading it fails, it falls back to `data/upcoming.txt` (`YYYY-MM-DD | Venue | City, ST`) and returns a `notice` for the site to show. Tested on 2026-11-13: it returned 2026-12-14, The Ardmore Music Hall, Ardmore, PA. The fallback was also tested.
The homepage only shows **one** next show, so run length isn't known ahead of time. Work out night-of-run from the shows already played at that venue in the current run.

---

## 2. The big picture

```
discobiscuits.net/mcp
        │  (Python script, API key from .env or GitHub Secret)
        ▼
  data/biscuits.db   ← SQLite database file, committed to the repo
        │  (Python script does the math)
        ▼
  site/  ← plain HTML + JSON files
        │
        ▼
  GitHub Pages (free) → open it on your phone
```

### Update schedule (user's choice: option C plus a manual button)

- **Show nights:** run every 30 minutes from 7pm to 3am Eastern. GitHub cron uses UTC, so schedule `*/30 0-8,23 * * *` to cover both EST and EDT. The script exits right away if no show is scheduled for that night.
- **Every other day:** one run around 9am Eastern (`0 14 * * *`).
- **Manual:** `workflow_dispatch` adds a **"Run workflow"** button in the GitHub Actions tab, which works from the phone too.
- Every run re-checks the **last 3 days** of shows to pick up songs posted late and corrections.
- A prediction gets scored only once its show is **final**, meaning the setlist hasn't changed for 12 hours. Before that, the site shows a "live / provisional" score.
- GitHub's scheduled runs can start 5 to 15 minutes late. That's expected.

Each run does these steps:
1. Ask the site for this year's shows.
2. If there's a show we don't have, fetch its setlist and add it to the database.
3. Recompute the stats and rebuild the website.
4. Commit the updated database and publish the site.

If there's no new show, the job stops early and does nothing.

---

## 3. Tools and libraries (all free)

- **Python 3** with `requests` (web calls), `python-dotenv` (reads `.env`), and `jinja2` (fills HTML templates).
- **SQLite**, which is built into Python. No server to install.
- **Plain HTML, CSS, and a little JavaScript** for the site. It must be mobile-first and readable on a phone.
- **GitHub Pages** for hosting and **GitHub Actions** for automation.

---

## 4. Folder layout

```
biscuits-oracle/
├── CLAUDE.md            ← this plan
├── .env                 ← API key (never committed)
├── .gitignore           ← lists .env so git ignores it
├── requirements.txt
├── data/
│   ├── biscuits.db      ← the database
│   └── predictions/     ← your prediction files (see section 7)
├── scripts/
│   ├── mcp_client.py    ← talks to discobiscuits.net, waits to respect the rate limit
│   ├── schema.sql       ← database table definitions
│   ├── backfill.py      ← one-time: pull ALL history
│   ├── update.py        ← daily: pull only new shows
│   ├── parse_notes.py   ← turns notes text into flags, teases, and sit-ins
│   ├── stats.py         ← all the math
│   └── build_site.py    ← writes the website files
├── templates/           ← HTML templates
├── site/                ← generated website (GitHub Pages serves this)
└── .github/workflows/update.yml
```

---

## 5. Database tables

| Table | What it holds |
|---|---|
| `venues` | id, name, city, state, country |
| `shows` | id, slug, date, venue_id, tour/run id, night of run (1, 2, 3…), show number (counts every show in order), rating |
| `runs` | Back-to-back shows at the same venue or festival (for example, 3 nights at the Capitol Theatre) |
| `tours` | Groups of shows by tour (from the site if it provides tours, otherwise worked out from date gaps) |
| `songs` | id, name, original artist, is_cover |
| `sets` | show_id, set name (S1/S2/E1…), `set_type`: `electric` (normal), `tractorbeam`, or `powered_down` (acoustic) |
| `performances` | One row per song played: show_id, set_id, position, song_id, raw notes text |
| `segues` | from_performance → to_performance, type (`>` segue or `->` jam) |
| `performance_flags` | performance_id, flag: `inverted`, `dyslexic`, `unfinished`, `beginning_only`, `middle_only`, `ending_only` |
| `completions` | Which performance finishes which. Example: the 2nd-set "Munchkin Invasion" completes the 1st-set one. Links the opener and the closer performances. |
| `teases` | performance_id, teased song name, song_id if it's a known song |
| `sit_ins` | show_id, performance_id (null means the whole show), guest name, instrument |
| `predictions` | id, show_id, made_at, predicted setlist (JSON), real setlist (JSON), score, score details |
| `sync_log` | When each fetch ran and what it added. Lets the update script know where it stopped. |

---

## 6. The math

A **"show gap"** counts shows, not days. Every stat below runs on the show numbers.

**Settings file:** every adjustable setting lives in `config.toml`, a plain text file of settings. Examples:
```toml
tour_gap_days = 21            # new tour after this many days with no show
include_tractorbeam = false   # count Tractorbeam sets in gap/rotation math?
include_powered_down = false  # count Powered Down acoustic sets in gap/rotation math?
bustout_min_gap = 100         # shows
recency_half_life_days = 365  # a show this many days old counts half as much as last night's

[[eras]]
name = "Marlon Lewis era"
start = "2025-10-31"          # Marlon Lewis became the drummer
# add earlier eras the same way if wanted
```
**Recency weighting:** each past show counts `0.5 ^ (age_in_days / recency_half_life_days)`. With the default of 365 days, a show 1 year ago counts 0.5, 2 years ago 0.25, and so on. This weighting applies to base rates and rotation in the prediction score. Gaps and bustouts always use the full history.

**Eras:** eras come from `config.toml`. Each show gets an era by its date. Every stats page has a toggle between **All time** and **Current era** (the newest era). In Current era mode, gaps, rotation, and venue stats only count shows in that era. A song never played in the current era is marked "not yet played this era."
**Special sets:** each set is tagged `electric`, `tractorbeam`, or `powered_down`. In step 3, find out how the API marks these: by set name, notes, or show title. A show made up only of excluded sets also isn't counted as a "show" for gap counting. The site pages show these sets with a label either way.

1. **Song gaps:** shows since each song was last played. **"Due"** means the current gap is larger than that song's usual gap (its median gap). Songs are ranked by `current gap ÷ median gap`.
2. **Recent rotation:** how many times each song was played this calendar year and this tour, and in the last 10, 20, and 50 shows.
3. **Open threads:** a song started but not finished (unfinished, inverted, or "beginning only") with no matching completion yet. We work this out from `performance_flags` and `completions`. An open thread gets a big score boost because the band often finishes it soon.
4. **Run awareness:** if a song was already played earlier in the same run, its score is multiplied by a small number (for example 0.05). The only exception is when it's an open thread waiting to be finished.
5. **Venue history:** what has been played at this venue before, and songs that are often or never played there. **Night of run:** which songs tend to show up on night 1 versus the last night.
6. **Bustout candidates:** songs with a gap of 100+ shows (adjustable) that have a reason to return. Possible reasons: the date is an anniversary, the song was played at this venue before, it's tied to a holiday or New Year's Eve, a related song was played recently, or the song has an open thread.
7. **Prediction score:** for each song, a weighted score built from the pieces above:
   `score = base_rate × due_factor × rotation_factor × venue_factor × night_factor × run_penalty + open_thread_bonus`
   The weights are numbers in one config file so they can be tuned. The site shows a ranked list with each score and *why* each song scored high.
8. **Prediction log scoring:** after a show, compare the prediction to the real setlist. Your predictions and the robot's are scored the same way. Show a running total and a head-to-head comparison.

   | What was right | Points | Setting |
   |---|---|---|
   | Predicted song was played (anywhere) | 1 | `pts_song` |
   | …and in the predicted set | +1 | `pts_right_set` |
   | Set 1 opener exactly right | +3 | `pts_s1_opener` |
   | Set 2 opener exactly right | +3 | `pts_s2_opener` |
   | Encore song right | +2 | `pts_encore` |
   | Segue pair right (A > B happened) | +2 | `pts_segue` |
   | Flag right (e.g. "unfinished") | +2 | `pts_flag` |
   | Bustout: correct song whose gap was ≥ `score_bustout_gap` (default 50) shows | +3 | `pts_bustout` |
   | Correct song that completes an earlier unfinished or inverted version from the same run | +3 | `pts_completion` |

   - Only the **first 15 songs** in the file count (`max_counted_songs = 15`). Recent shows have about 10 to 12 unique songs.
   - Also show the **hit rate**: correct songs ÷ songs counted.
   - All the points and thresholds live in `config.toml`. The scoring bustout gap (`score_bustout_gap`, 50) is a separate setting from the bustout-candidates list (`bustout_min_gap`, 100).

---

## 7. Prediction log: how you'll add one

GitHub Pages sites are read-only, so you can't save a prediction from the website itself.
The plan is to write your prediction as a small text file in `data/predictions/` (for example `2026-11-20.txt`, one song per line).
You can do this from your phone on github.com.
The daily job loads it, scores it after the real show comes in, and shows the results on the site.

### Exact prediction file format (v1)

- **File name:** `data/predictions/YYYY-MM-DD.txt`, using the show date. If there are two shows on one date, use `YYYY-MM-DD-2.txt` for the second.
- **Encoding:** plain text, UTF-8.

```
show: 2026-11-20
venue: Capitol Theatre
made: 2026-11-19

[Set 1]
Save the Robots >
Humuhumunukunukuapua'a >
The Bells
Sound One

[Set 2]
Vassillios >
Munchkin Invasion (unfinished)
Spacebirdmatingcall

[Encore]
One Day
```

Rules:
1. The first lines are `key: value` headers. `show:` is required. `venue:` and `made:` are optional.
2. Set headers are exactly `[Set 1]`, `[Set 2]`, `[Set 3]`, or `[Encore]`. (`[Encore 2]` is allowed.)
3. Put one song per line, in the order you expect it.
4. A song name should match discobiscuits.net's spelling. The parser also accepts close spellings and common nicknames, and it warns about anything it can't match.
5. A line ending in ` >` means "this song segues into the next one." This is optional.
6. A flag in parentheses after the name is optional: `(inverted)`, `(dyslexic)`, `(unfinished)`, `(beginning only)`, `(middle only)`, or `(ending only)`.
7. Lines starting with `#` are comments and are ignored. Blank lines are ignored.
8. A prediction is locked once the show starts. The job only scores files committed **before** the show date (it uses the git commit time), so nobody can edit a prediction after the fact.

**Prompt to give the other Claude chat:** "Write the prediction in the Biscuits Oracle v1 format: headers `show:`, `venue:`, `made:`, then `[Set 1]` / `[Set 2]` / `[Encore]` sections, one song per line using discobiscuits.net spellings, optional ` >` for segues, optional `(flag)`. Output only the file contents in one code block."

---

## 8. Website pages (mobile-first)

- **Home / Next Show:** the next show if one is known, a ranked prediction list with reasons, open threads, and the current run so far.
- **Songs:** a sortable table with gap, median gap, plays this year and this tour, last played, and a due flag.
- **Song detail:** play history, gap chart, and flags history.
- **Bustouts:** candidates with their reasons.
- **Venues:** each venue's history and night-of-run patterns.
- **Shows:** a list and setlist pages with segues, flags, teases, and sit-ins.
- **My Predictions:** the log and scores.

---

## 9. Step-by-step build order

Each step ends with something the user can see working.

1. **Accounts and keys:** create the API key on discobiscuits.net and save it in `.env`. Create the `.gitignore`.
2. **Project setup:** make a Python virtual environment, run `pip install`, then `git init` and connect to a new GitHub repo.
3. **Explore the data:** a tiny script calls `tools/list` and a few `get_setlist` calls, and saves sample responses. We read real `notes` text to learn how flags, teases, and sit-ins are written.
4. **Database schema:** create `biscuits.db` with the tables from section 5.
5. **Backfill:** fetch every year from the first show (around 1995) to today, then all setlists in batches. Stay under 60 requests per minute. If the script stops partway, it resumes. Expect it to take a while.
6. **Notes parser:** turn the notes text into flags, completions, teases, and sit-ins. Spot-check the results against the website.
7. **Stats:** build `stats.py` and check a few numbers against discobiscuits.net's own gap info.
8. **Website:** generate the pages and preview them on your computer.
9. **GitHub Pages:** publish the site and open it on your phone.
10. **Automation:** add the API key as a GitHub Secret, then add the daily Actions workflow. Test it by running it manually.
11. **Predictions:** add the prediction file format, scoring, and log page.
12. **Tuning:** adjust the score weights by back-testing. To back-test, pretend it's a past date, predict that show, and compare the prediction to what was really played.

---

## 10. Safety rules

- `.env` must be in `.gitignore` **before** the first commit.
- In GitHub Actions, the key comes from `secrets.DISCOBISCUITS_API_KEY` and is never printed.
- The script stays under 60 requests per minute and identifies itself with a generic User-Agent.
- The site is personal and non-commercial, with credit and a link to discobiscuits.net on every page.

---

## 11. Open questions for the user

(Answers go here as they come in.)

- **Name:** the repo is `biscuits-oracle` and the site title is "Biscuits Oracle". The address will be `https://<username>.github.io/biscuits-oracle/`. Planning finished 2026-11-13.
- **Update frequency:** option C. Run every 30 minutes on show nights and once a day otherwise. Also add a manual `workflow_dispatch` "Run workflow" button.
- **Scoring:** the table in section 6.8, with a 15-song cap, a +3 bustout bonus for gaps of 50+ shows, and +3 for calling a same-run completion.
- **What should the prediction look like?** Option B: a ranked song list plus a full predicted setlist (sets, order, segues). The predicted setlist is saved in the v1 prediction format as `data/predictions/robot/YYYY-MM-DD.txt` and scored the same way as the user's, so the two can be compared head to head. Also show the Option C slot picks: top 3 Set 1 openers, top 3 Set 2 openers, top 3 encores, and the likeliest segue pairs.
- **How much old history counts?** Option C: all history, with recent shows weighted more. The half-life is adjustable and starts at 1 year. There's also an era marker: the Marlon Lewis era starts 2025-10-31, and stats pages can be viewed for the current era only.
- **What is a "tour"?** Option B: use tour names from the API if it has them. Otherwise, start a new tour after a gap of more than `tour_gap_days` with no show. The default is 21, set in `config.toml`.
- **Special sets:** tag Tractorbeam sets and Powered Down (acoustic) sets separately, so they can be included in or excluded from rotation and gap math. Both are switches in `config.toml`.
- **Powered Down tagging:** option A. Keep a hand-kept list in `config.toml`, seeded by a backfill scan of notes, annotations, band name or billing differences, and lineup differences. Give the user the candidates as a plain-text list to confirm.
- **How does the site know the next show?** The API has no future dates, so the user chose to read the homepage's "Next Show" box. If that fails, fall back to `data/upcoming.txt` and show a notice on the site. This replaces the earlier "no fallback" answer.
- **How to enter predictions?** Option A for now: a text file in `data/predictions/`. Later, add Option C: a form on the site that opens a ready-to-commit file on github.com. Predictions usually come from a separate Claude chat, so the format below must be exact and easy to paste.
- **Public repo?** Yes. The GitHub repository will be public, which is needed for free GitHub Pages. The API key stays private in `.env` and GitHub Secrets.
