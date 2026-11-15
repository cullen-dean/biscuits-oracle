"""Step 7: all the math.

    python scripts/stats.py            -> update show numbers / runs / tours, print a stats report
    python scripts/stats.py --check    -> also compare our counts with discobiscuits.net (a few API calls)

Other scripts use compute(conn, cfg, next_show) to get everything as plain Python data.

Words used here:
  counted show   a show that counts for gaps: countForStats is true and it has at least one
                 counted song (Tractorbeam / Powered Down / side-project sets are left out
                 unless config.toml says to include them). Shows with NO setlist in the API
                 (mostly 1996-98, plus placeholders) also count, like on discobiscuits.net,
                 unless their notes say Tractorbeam. They're left out of play-rate math.
  show number    1, 2, 3 ... over counted shows in date order.
  gap            counted shows IN BETWEEN two plays (back-to-back shows = 0), the site's convention.
  current gap    counted shows since the song was last played (0 = played at the latest show),
                 the same as the site's "N shows ago". If played at the next show, its gap = current gap.
  due ratio      (current gap + 1) / (median gap + 1): over 1 means the song is past its usual gap.
                 (The +1 keeps songs with a median gap of 0 from dividing by zero.)
"""

import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db import connect, init_db, load_config  # noqa: E402

HOLIDAYS = {"12-31": "New Year's Eve", "10-31": "Halloween", "07-04": "the 4th of July"}


def d(s):
    return date.fromisoformat(s)


def norm(s):
    s = (s or "").lower().replace("’", "'")
    s = re.sub(r"^the\s+", "", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def clip(x, lo, hi):
    return max(lo, min(hi, x))


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def is_counted_perf(set_type, is_tb, cfg):
    if set_type == "tractorbeam" or is_tb == 2:
        return cfg["include_tractorbeam"]
    if set_type == "powered_down":
        return cfg["include_powered_down"]
    if set_type == "side_project":
        return cfg.get("include_side_project", False)
    return True


def load(conn, cfg):
    shows = [dict(r) for r in conn.execute("""
        SELECT sh.id, sh.slug, sh.date, sh.day_order, sh.venue_id, sh.count_for_stats, sh.notes,
               v.name AS venue, v.city, v.state
        FROM shows sh LEFT JOIN venues v ON v.id = sh.venue_id""")]
    shows.sort(key=lambda s: (s["date"], s["day_order"] or 1, s["id"]))
    by_id = {s["id"]: s for s in shows}

    perfs = [dict(r) for r in conn.execute("""
        SELECT p.id, p.show_id, p.song_id, p.position, p.segue_out, p.is_tractorbeam,
               s.label AS set_label, s.set_order, s.set_type
        FROM performances p JOIN sets s ON s.id = p.set_id""")]
    flags = defaultdict(set)
    for r in conn.execute("SELECT perf_id, flag FROM performance_flags"):
        flags[r[0]].add(r[1])
    completed = {r[0] for r in conn.execute(
        "SELECT completed_perf_id FROM completions WHERE completed_perf_id IS NOT NULL")}
    songs = {r["id"]: dict(r) for r in conn.execute("SELECT id, slug, title FROM songs")}

    for p in perfs:
        p["counted"] = bool(by_id[p["show_id"]]["count_for_stats"]) and \
            is_counted_perf(p["set_type"], p["is_tractorbeam"], cfg)
        p["flags"] = flags.get(p["id"], set())

    order = {s["id"]: i for i, s in enumerate(shows)}
    perfs.sort(key=lambda p: (order[p["show_id"]], p["set_order"], p["position"]))
    return shows, by_id, perfs, completed, songs


# ---------------------------------------------------------------------------
# Show numbers, runs, tours, eras  (written back to the database)
# ---------------------------------------------------------------------------

def no_setlist_show_counts(s, cfg):
    """A show with no setlist in the API counts for gaps unless it's a special show we exclude."""
    if not s["count_for_stats"]:
        return False
    if re.search(r"tractor\s*beam", s["notes"] or "", re.I) and not cfg["include_tractorbeam"]:
        return False
    for key, include in (("powered_down", "include_powered_down"), ("side_project", "include_side_project")):
        listed = {e.split()[0] for e in cfg.get(key, [])}
        if (s["date"] in listed or s["slug"] in listed) and not cfg.get(include, False):
            return False
    return True


def derive_shows(conn, cfg, shows, perfs):
    counted_ids = {p["show_id"] for p in perfs if p["counted"]}
    has_perfs = {p["show_id"] for p in perfs}
    n = 0
    for s in shows:
        s["has_setlist"] = s["id"] in has_perfs
        s["counted"] = s["id"] in counted_ids or (not s["has_setlist"] and no_setlist_show_counts(s, cfg))
        if s["counted"]:
            n += 1
            s["show_number"] = n
        else:
            s["show_number"] = None

    # Runs: shows at the same venue no more than run_max_days_between days apart.
    max_days = cfg.get("run_max_days_between", 2)
    runs, open_run = [], {}  # open_run: venue_id -> run dict
    for s in shows:
        if s["venue_id"] is None:  # placeholder shows: no venue, so no run
            s["night_of_run"], s["run"] = 1, None
            continue
        r = open_run.get(s["venue_id"])
        if r and (d(s["date"]) - d(r["end"])).days <= max_days:
            r["shows"].append(s)
            r["end"] = s["date"]
        else:
            r = {"venue_id": s["venue_id"], "start": s["date"], "end": s["date"], "shows": [s]}
            runs.append(r)
            open_run[s["venue_id"]] = r
        s["night_of_run"] = len(r["shows"])
        s["run"] = r

    # Tours: a new tour after more than tour_gap_days with no show.
    tours, cur = [], None
    for s in shows:
        if cur is None or (d(s["date"]) - d(cur["end"])).days > cfg["tour_gap_days"]:
            cur = {"start": s["date"], "end": s["date"], "shows": []}
            tours.append(cur)
        cur["shows"].append(s)
        cur["end"] = s["date"]
        s["tour"] = cur
    for t in tours:
        a, b = d(t["start"]), d(t["end"])
        t["name"] = f"{a:%b %Y}" if (a.year, a.month) == (b.year, b.month) else \
            (f"{a:%b}–{b:%b %Y}" if a.year == b.year else f"{a:%b %Y}–{b:%b %Y}")

    # Eras from config (newest last). Shows before the first era are "Before <first era>".
    eras = sorted(cfg.get("eras", []), key=lambda e: e["start"])
    for s in shows:
        s["era"] = None
        for e in eras:
            if s["date"] >= e["start"]:
                s["era"] = e["name"]

    # Write back.
    conn.execute("UPDATE shows SET run_id = NULL, tour_id = NULL")
    conn.execute("DELETE FROM runs")
    conn.execute("DELETE FROM tours")
    for r in runs:
        r["id"] = conn.execute("INSERT INTO runs (venue_id, start_date, end_date, n_shows) VALUES (?, ?, ?, ?)",
                               (r["venue_id"], r["start"], r["end"], len(r["shows"]))).lastrowid
    for t in tours:
        t["id"] = conn.execute("INSERT INTO tours (name, start_date, end_date, source) VALUES (?, ?, ?, 'gap')",
                               (t["name"], t["start"], t["end"])).lastrowid
    for s in shows:
        conn.execute("UPDATE shows SET show_number = ?, run_id = ?, night_of_run = ?, tour_id = ? WHERE id = ?",
                     (s["show_number"], s["run"]["id"] if s["run"] else None, s["night_of_run"],
                      s["tour"]["id"], s["id"]))
    conn.commit()
    return runs, tours


# ---------------------------------------------------------------------------
# Song stats
# ---------------------------------------------------------------------------

def song_stats(shows, perfs, songs, mode_shows, today):
    """Gap / rotation stats over the given counted shows (all-time or one era)."""
    idx = {s["id"]: i for i, s in enumerate(mode_shows)}
    N = len(mode_shows)
    latest = mode_shows[-1] if mode_shows else None
    this_year = str(today.year)
    tour = latest["tour"] if latest else None

    played_at = defaultdict(list)     # song -> sorted unique show indexes
    times = Counter()                 # song -> performances
    for p in perfs:
        if p["counted"] and p["show_id"] in idx:
            times[p["song_id"]] += 1
            i = idx[p["show_id"]]
            if not played_at[p["song_id"]] or played_at[p["song_id"]][-1] != i:
                played_at[p["song_id"]].append(i)

    out = {}
    for song_id, idxs in played_at.items():
        gaps = [b - a - 1 for a, b in zip(idxs, idxs[1:])]  # shows in between
        median_gap = statistics.median(gaps) if gaps else None
        current_gap = (N - 1) - idxs[-1]
        # If played at the next show, the song's gap would be current_gap. Compare with its usual gap.
        due_ratio = (current_gap + 1) / (median_gap + 1) if median_gap is not None else None
        played_shows = [mode_shows[i] for i in idxs]
        out[song_id] = {
            "song_id": song_id,
            "title": songs[song_id]["title"],
            "slug": songs[song_id]["slug"],
            "times_played": times[song_id],
            "shows_played": len(idxs),
            "first_played": played_shows[0]["date"],
            "last_played": played_shows[-1]["date"],
            "current_gap": current_gap,
            "median_gap": median_gap,
            "avg_gap": statistics.mean(gaps) if gaps else None,
            "due_ratio": due_ratio,
            "is_due": bool(due_ratio and due_ratio > 1),
            "this_year": sum(1 for s in played_shows if s["date"][:4] == this_year),
            "this_tour": sum(1 for s in played_shows if s["tour"] is tour),
            "last_10": sum(1 for i in idxs if i >= N - 10),
            "last_20": sum(1 for i in idxs if i >= N - 20),
            "last_50": sum(1 for i in idxs if i >= N - 50),
        }
    return out


# ---------------------------------------------------------------------------
# Open threads
# ---------------------------------------------------------------------------

def completion_chances(cfg, counted_shows, perfs, completed_by):
    """Historical chance that an open thread gets completed exactly k shows later (k = 1..window),
    given it wasn't completed before then."""
    flags = set(cfg["open_thread_flags"])
    window = cfg["open_thread_window"]
    num = {s["id"]: s["show_number"] for s in counted_shows}
    at_risk, done = Counter(), Counter()
    for p in perfs:
        if not (p["flags"] & flags) or p["show_id"] not in num:
            continue
        lag = completed_by.get(p["id"])  # shows until completion, or None
        for k in range(1, window + 1):
            at_risk[k] += 1
            if lag == k:
                done[k] += 1
                break
    return {k: (done[k] / at_risk[k] if at_risk[k] else 0.0) for k in range(1, window + 1)}


def open_threads(cfg, counted_shows, perfs, completed, songs):
    flags = set(cfg["open_thread_flags"])
    window = cfg["open_thread_window"]
    N = len(counted_shows)
    num = {s["id"]: s["show_number"] for s in counted_shows}
    by_id = {s["id"]: s for s in counted_shows}
    out = []
    for p in perfs:
        if p["flags"] & flags and p["id"] not in completed and p["show_id"] in num:
            age = N - num[p["show_id"]] + 1  # 1 = from the latest show
            if age <= window:
                s = by_id[p["show_id"]]
                out.append({"perf_id": p["id"], "song_id": p["song_id"], "title": songs[p["song_id"]]["title"],
                            "date": s["date"], "set": p["set_label"], "position": p["position"],
                            "flags": sorted(p["flags"]), "shows_ago": age - 1, "age": age})
    return out


# ---------------------------------------------------------------------------
# Next-show context: run, night, venue
# ---------------------------------------------------------------------------

def match_venue(conn, name, location):
    if not name:
        return None
    city = (location or "").split(",")[0]
    rows = conn.execute("SELECT id, name, city FROM venues").fetchall()
    exact = [r["id"] for r in rows if norm(r["name"]) == norm(name) and norm(r["city"]) == norm(city)]
    if exact:
        return exact[0]
    loose = [r["id"] for r in rows if norm(r["name"]) == norm(name)]
    return loose[0] if len(loose) == 1 else None


def next_show_context(conn, cfg, next_show, shows):
    if not next_show or not next_show.get("date"):
        return None
    next_show = {**next_show, "date": str(next_show["date"])[:10]}  # may arrive as a date object
    ctx = {"date": next_show["date"], "venue": next_show.get("venue"), "location": next_show.get("location"),
           "venue_id": match_venue(conn, next_show.get("venue"), next_show.get("location")),
           "run_shows": [], "night_of_run": 1}
    played = [s for s in shows if s["date"] < next_show["date"]]
    if played and ctx["venue_id"]:
        last = played[-1]
        if last["venue_id"] == ctx["venue_id"] and \
                (d(next_show["date"]) - d(last["date"])).days <= cfg.get("run_max_days_between", 2):
            ctx["run_shows"] = last["run"]["shows"]
            ctx["night_of_run"] = last["night_of_run"] + 1
    return ctx


# ---------------------------------------------------------------------------
# Prediction score
# ---------------------------------------------------------------------------

def predict(cfg, counted_shows, perfs, songs, stats_all, threads, chances, ctx, ref_date):
    lo, hi = cfg["factor_min"], cfg["factor_max"]
    half_life = cfg["recency_half_life_days"]
    # Play rates only make sense over shows whose setlist we know.
    counted_shows = [s for s in counted_shows if s["has_setlist"]]
    show_weight = {s["id"]: 0.5 ** (max(0, (ref_date - d(s["date"])).days) / half_life) for s in counted_shows}
    total_w = sum(show_weight.values())

    song_shows = defaultdict(set)
    for p in perfs:
        if p["counted"] and p["show_id"] in show_weight:
            song_shows[p["song_id"]].add(p["show_id"])
    base = {sid: sum(show_weight[s] for s in ss) / total_w for sid, ss in song_shows.items()}

    # Venue and night-of-run rates (unweighted, all counted shows).
    by_id = {s["id"]: s for s in counted_shows}
    venue_id = ctx["venue_id"] if ctx else None
    night_cat = "later" if ctx and ctx["night_of_run"] > 1 else "first"
    venue_shows = [s for s in counted_shows if venue_id and s["venue_id"] == venue_id]
    cat_shows = [s for s in counted_shows if (s["night_of_run"] > 1) == (night_cat == "later")]
    n_counted = len(counted_shows)
    run_song_ids = set()
    if ctx:
        run_ids = {s["id"] for s in ctx["run_shows"]}
        run_song_ids = {p["song_id"] for p in perfs if p["show_id"] in run_ids and p["counted"]}
    thread_by_song = {}
    for t in threads:
        if t["song_id"] not in thread_by_song or t["age"] < thread_by_song[t["song_id"]]["age"]:
            thread_by_song[t["song_id"]] = t

    rows = []
    for sid, b in base.items():
        st = stats_all.get(sid)
        if not st or songs[sid]["slug"] == "unknown":
            continue
        why = []
        raw_rate = st["shows_played"] / n_counted

        due = 1.0
        if st["due_ratio"] is not None:
            due = clip(st["due_ratio"], lo, hi) ** cfg["w_due"]
            if due >= 1.3:
                why.append(f"due: {st['current_gap']} shows since last played vs usual gap of {st['median_gap']:g}")
            elif due <= 0.5:
                ago = "at the last show" if st["current_gap"] == 0 else \
                    f"{st['current_gap']} show{'s' if st['current_gap'] != 1 else ''} ago"
                why.append(f"played recently ({ago}; usual gap {st['median_gap']:g})")

        rot = clip((st["last_20"] + 1) / (20 * b + 1), lo, hi) ** cfg["w_rotation"]
        if rot >= 1.3:
            why.append(f"hot right now: {st['last_20']} of the last 20 shows")
        elif rot <= 0.6:
            why.append(f"cold lately: {st['last_20']} of the last 20 shows")

        ven = 1.0
        if venue_shows:
            k = 3
            plays_here = len(song_shows[sid] & {s["id"] for s in venue_shows})
            rate_here = (plays_here + k * raw_rate) / (len(venue_shows) + k)
            ven = clip(rate_here / raw_rate, lo, hi) ** cfg["w_venue"] if raw_rate else 1.0
            if plays_here >= 2 and ven >= 1.15:
                why.append(f"venue favorite: {plays_here} of {len(venue_shows)} shows here")

        k = 10
        plays_cat = len(song_shows[sid] & {s["id"] for s in cat_shows})
        rate_cat = (plays_cat + k * raw_rate) / (len(cat_shows) + k)
        night = clip(rate_cat / raw_rate, lo, hi) ** cfg["w_night"] if raw_rate else 1.0
        if night >= 1.15:
            why.append(f"likes {'later nights' if night_cat == 'later' else 'first nights'} of runs")

        pen = 1.0
        thread = thread_by_song.get(sid)
        if sid in run_song_ids and not thread:
            pen = cfg["run_repeat_penalty"]
            why.append("already played earlier in this run")

        bonus = 0.0
        if thread:
            chance = chances.get(thread["age"], 0.0)
            bonus = chance * cfg["w_open_thread"]
            why.append(f"open thread: {'/'.join(f.lower() for f in thread['flags'])} on {thread['date']} "
                       f"({chance:.0%} of threads this old get completed next show)")

        score = b * due * rot * ven * night * pen + bonus
        rows.append({"song_id": sid, "title": st["title"], "score": score, "base_rate": b,
                     "due": due, "rotation": rot, "venue": ven, "night": night, "run_penalty": pen,
                     "open_thread_bonus": bonus, "why": why})
    rows.sort(key=lambda r: -r["score"])
    return rows


# ---------------------------------------------------------------------------
# Bustouts
# ---------------------------------------------------------------------------

def bustouts(conn, cfg, counted_shows, perfs, stats_all, threads, ctx, ref_date):
    min_gap = cfg["bustout_min_gap"]
    by_id = {s["id"]: s for s in counted_shows}
    song_dates = defaultdict(list)
    song_venues = defaultdict(set)
    for p in perfs:
        if p["counted"] and p["show_id"] in by_id:
            s = by_id[p["show_id"]]
            song_dates[p["song_id"]].append(s["date"])
            song_venues[p["song_id"]].add(s["venue_id"])
    recent_ids = [s["id"] for s in counted_shows[-20:]]
    teased = defaultdict(list)
    q = "SELECT t.song_id, sh.date FROM teases t JOIN performances p ON p.id = t.perf_id " \
        "JOIN shows sh ON sh.id = p.show_id WHERE t.song_id IS NOT NULL AND p.show_id IN (%s)" % \
        ",".join("?" * len(recent_ids))
    for sid, dt in conn.execute(q, recent_ids):
        teased[sid].append(dt)
    thread_songs = {t["song_id"] for t in threads}
    target_md = ctx["date"][5:] if ctx else ref_date.strftime("%m-%d")
    target = d(ctx["date"]) if ctx else ref_date

    out = []
    for sid, st in stats_all.items():
        if st["current_gap"] < min_gap:
            continue
        reasons = []
        debut = d(st["first_played"])
        try:
            anniv = debut.replace(year=target.year)
        except ValueError:  # Feb 29
            anniv = debut.replace(year=target.year, day=28)
        if abs((anniv - target).days) <= 3:
            reasons.append(f"debut anniversary ({st['first_played']})")
        same_day = [x for x in song_dates[sid] if x[5:] == target_md]
        if same_day and not reasons:
            reasons.append(f"played on this date before ({same_day[-1]})")
        if ctx and ctx["venue_id"] in song_venues[sid]:
            reasons.append("played at this venue before")
        if target_md in HOLIDAYS and any(x[5:] == target_md for x in song_dates[sid]):
            reasons.append(f"a past {HOLIDAYS[target_md]} song")
        if teased[sid]:
            reasons.append(f"teased recently ({teased[sid][-1]})")
        if sid in thread_songs:
            reasons.append("has an open thread")
        out.append({**st, "reasons": reasons})
    out.sort(key=lambda r: (-len(r["reasons"]), -r["current_gap"]))
    return out


# ---------------------------------------------------------------------------

def compute(conn, cfg, next_show=None, today=None):
    today = today or date.today()
    shows, by_id, perfs, completed, songs = load(conn, cfg)
    runs, tours = derive_shows(conn, cfg, shows, perfs)
    counted = [s for s in shows if s["counted"]]
    eras = sorted(cfg.get("eras", []), key=lambda e: e["start"])
    current_era = eras[-1]["name"] if eras else None
    era_shows = [s for s in counted if current_era and s["era"] == current_era]

    stats_all = song_stats(shows, perfs, songs, counted, today)
    stats_era = song_stats(shows, perfs, songs, era_shows, today) if era_shows else {}

    # How many counted shows until each completion (for completion chances).
    num = {s["id"]: s["show_number"] for s in counted}
    perf_show = {p["id"]: p["show_id"] for p in perfs}
    completed_by = {}
    for completed_id, completing_id in conn.execute(
            "SELECT completed_perf_id, completing_perf_id FROM completions WHERE completed_perf_id IS NOT NULL"):
        a, b = num.get(perf_show.get(completed_id)), num.get(perf_show.get(completing_id))
        if a and b and b > a:
            completed_by[completed_id] = min(b - a, completed_by.get(completed_id, 10 ** 9))
    chances = completion_chances(cfg, counted, perfs, completed_by)
    threads = open_threads(cfg, counted, perfs, completed, songs)

    ctx = next_show_context(conn, cfg, next_show, shows)
    ref_date = d(ctx["date"]) if ctx else today
    prediction = predict(cfg, counted, perfs, songs, stats_all, threads, chances, ctx, ref_date)
    busts = bustouts(conn, cfg, counted, perfs, stats_all, threads, ctx, ref_date)

    return {
        "shows": shows, "counted_shows": counted, "runs": runs, "tours": tours,
        "current_era": current_era, "era_shows": era_shows,
        "songs_all": stats_all, "songs_era": stats_era,
        "open_threads": threads, "completion_chances": chances,
        "next_show": ctx, "prediction": prediction, "bustouts": busts,
    }


# ---------------------------------------------------------------------------
# Report + check against discobiscuits.net
# ---------------------------------------------------------------------------

def check_against_site(conn, cfg, result, slugs):
    from mcp_client import MCPClient
    client = MCPClient()
    print("\n===== CHECK AGAINST discobiscuits.net =====")
    print("The site's 'times played' = number of SHOWS with the song (countForStats shows, every set type).")
    rows = conn.execute("""
        SELECT so.slug, COUNT(DISTINCT p.show_id) n, MIN(sh.date) first, MAX(sh.date) last
        FROM performances p JOIN shows sh ON sh.id = p.show_id JOIN songs so ON so.id = p.song_id
        WHERE sh.count_for_stats = 1 GROUP BY so.slug""").fetchall()
    ours = {r["slug"]: r for r in rows}
    yearly = defaultdict(Counter)
    for slug, _, y in conn.execute("""SELECT DISTINCT so.slug, sh.id, substr(sh.date,1,4) FROM performances p
            JOIN shows sh ON sh.id = p.show_id JOIN songs so ON so.id = p.song_id WHERE sh.count_for_stats = 1"""):
        yearly[slug][y] += 1
    # Gap counting every countForStats show (how the site probably counts), for the browser check.
    stat_shows = [s for s in result["shows"] if s["count_for_stats"]]
    played = defaultdict(set)
    for slug, show_id in conn.execute("SELECT so.slug, p.show_id FROM performances p JOIN songs so ON so.id = p.song_id"):
        played[slug].add(show_id)
    slug_to_id = {st["slug"]: sid for sid, st in result["songs_all"].items()}
    ok = bad = 0
    for slug in slugs:
        site = client.call("get_song_statistics", songSlug=slug)["statistics"]
        o = ours.get(slug)
        site_years = {k: v for k, v in (site.get("yearlyPlayData") or {}).items()}
        year_diff = {y: (site_years.get(y, 0), yearly[slug].get(y, 0))
                     for y in set(site_years) | set(yearly[slug]) if site_years.get(y, 0) != yearly[slug].get(y, 0)}
        same = (o and site["timesPlayed"] == o["n"] and site["dateFirstPlayed"][:10] == o["first"]
                and site["dateLastPlayed"][:10] == o["last"] and not year_diff)
        ok += bool(same)
        bad += not same
        last_i = max((i for i, s in enumerate(stat_shows) if s["id"] in played[slug]), default=None)
        gap_site = len(stat_shows) - 1 - last_i if last_i is not None else None
        st = result["songs_all"].get(slug_to_id.get(slug))
        print(f"  {'OK  ' if same else 'DIFF'} {slug:<24} site {site['timesPlayed']:>4} shows "
              f"{site['dateFirstPlayed'][:10]}..{site['dateLastPlayed'][:10]} | ours {o['n'] if o else 0:>4} "
              f"{o['first'] if o else '-'}..{o['last'] if o else '-'} | current gap: {gap_site} "
              f"(all stat shows), {st['current_gap'] if st else '-'} (ours)"
              + (f" | year diffs (site, ours): {dict(sorted(year_diff.items()))}" if year_diff else ""))
    print(f"  {ok} match, {bad} differ")


def report(result):
    counted = result["counted_shows"]
    print("===== STATS =====")
    print(f"Counted shows: {len(counted)} of {len(result['shows'])} "
          f"(latest: {counted[-1]['date']} {counted[-1]['venue']})")
    print(f"Runs: {len(result['runs'])}, tours: {len(result['tours'])}, "
          f"current tour: {counted[-1]['tour']['name']} ({len(counted[-1]['tour']['shows'])} shows)")
    print(f"Current era: {result['current_era']} ({len(result['era_shows'])} counted shows)")
    ctx = result["next_show"]
    if ctx:
        run = f"night {ctx['night_of_run']} of a run" if ctx["run_shows"] else "not continuing a run (night 1)"
        print(f"Next show: {ctx['date']} {ctx['venue']}, {ctx['location']} "
              f"(venue {'matched' if ctx['venue_id'] else 'NEW / not matched'}; {run})")

    print("\nMost due ((current gap + 1) / (median gap + 1), songs played 10+ times, gap under 100):")
    due = [s for s in result["songs_all"].values()
           if s["due_ratio"] and s["shows_played"] >= 10 and s["current_gap"] < 100]
    for s in sorted(due, key=lambda s: -s["due_ratio"])[:10]:
        print(f"  {s['title']:<30} gap {s['current_gap']:>3}  median {s['median_gap']:>5g}  "
              f"ratio {s['due_ratio']:.1f}  last {s['last_played']}")

    print(f"\nOpen threads (last {len(result['completion_chances'])} shows):")
    for t in result["open_threads"]:
        print(f"  {t['title']:<30} {'/'.join(t['flags']).lower():<14} {t['date']} {t['set']} "
              f"({t['shows_ago']} shows ago)")
    print("  completion chance by age: " +
          ", ".join(f"{k} show{'s' if k > 1 else ''}: {v:.0%}" for k, v in result["completion_chances"].items()))

    print("\nPrediction (top 20):")
    for i, r in enumerate(result["prediction"][:20], 1):
        print(f"  {i:>2}. {r['title']:<30} {r['score']:.3f}  " + ("; ".join(r["why"]) or "steady rotation"))

    print("\nBustout candidates (top 10):")
    for s in result["bustouts"][:10]:
        print(f"  {s['title']:<30} gap {s['current_gap']:>4}  last {s['last_played']}  "
              + ("; ".join(s["reasons"]) or "-"))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from next_show import find_next_show
    conn = connect()
    init_db(conn)
    cfg = load_config()
    result = compute(conn, cfg, find_next_show())
    report(result)
    if "--check" in sys.argv:
        check_against_site(conn, cfg, result, [
            "basis-for-a-day", "helicopters", "crickets", "spacebirdmatingcall", "munchkin-invasion",
            "save-the-robots", "orch-theme", "quad-d", "voices-insane", "gamma-goblins"])
