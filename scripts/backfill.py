"""Step 5: pull the whole Disco Biscuits history into data/biscuits.db.

    python scripts/backfill.py            -> fetch anything missing, then load the database
    python scripts/backfill.py --load     -> skip fetching; rebuild the database from the cache

How it works (and why it can resume):
  1. List every year's shows          -> data/cache/years/YYYY.json
  2. Fetch setlists + show details    -> data/cache/setlists/<slug>.json, data/cache/shows/<slug>.json
     Anything already in the cache is not fetched again.
  3. Load the cache into the database (safe to re-run; it replaces each show's rows).
  4. Scan for possible Powered Down sets -> data/powered_down_candidates.txt
"""

import hashlib
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import parse_notes  # noqa: E402
from db import ROOT, connect, init_db, load_config  # noqa: E402

CACHE = ROOT / "data" / "cache"
YEARS_DIR = CACHE / "years"
SETLISTS_DIR = CACHE / "setlists"
SHOWS_DIR = CACHE / "shows"
CANDIDATES_PATH = ROOT / "data" / "powered_down_candidates.txt"

FIRST_YEAR = 1993  # a little before the first known show, just to be safe
BATCH = 25
PLACEHOLDER_SLUG = re.compile(r"^\d{4}-\d{2}-\d{2}(-\d+)?$")
PD_WORDS = re.compile(r"acoustic|powered[ -]?down|unplugged|stripped|seated", re.I)
BILLING_WORDS = re.compile(r"\b(billed as|performing as|performed as|under the name)\b", re.I)
IGNORED_INSTRUMENTS = {"vocals", "midi"}  # these come and go without meaning anything

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def cache_name(slug):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", slug) + ".json"


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def call_with_retry(client, tool, tries=3, **args):
    for attempt in range(tries):
        try:
            return client.call(tool, **args)
        except Exception as e:  # network hiccups, timeouts, server errors
            if attempt == tries - 1:
                raise
            log(f"  {tool} failed ({type(e).__name__}: {str(e)[:120]}); retrying in {10 * (attempt + 1)}s")
            time.sleep(10 * (attempt + 1))


def fetch_years(client, problems):
    this_year = date.today().year
    for year in range(FIRST_YEAR, this_year + 1):
        path = YEARS_DIR / f"{year}.json"
        # Past years never change, so reuse the cache. Always refresh the current year.
        if path.exists() and year < this_year:
            continue
        try:
            data = call_with_retry(client, "get_shows_by_year", year=year, limit=500)
        except Exception as e:
            problems.append(f"year {year}: listing failed: {e}")
            continue
        write_json(path, data)
        n = len(data.get("shows", [])) if isinstance(data, dict) else 0
        log(f"year {year}: {n} shows listed")
        if n >= 500:
            problems.append(f"year {year}: hit the 500-show limit, list may be cut off")


def listed_shows():
    """All real shows from the cached year lists, placeholders removed."""
    shows, placeholders = {}, []
    for path in sorted(YEARS_DIR.glob("*.json")):
        data = read_json(path)
        for s in data.get("shows", []) if isinstance(data, dict) else []:
            if not (s.get("venueName") or "").strip() or PLACEHOLDER_SLUG.match(s["slug"]):
                placeholders.append(s["slug"])
                continue
            shows[s["slug"]] = s
    return shows, placeholders


def fetch_batches(client, slugs, tool, arg_name, result_key, out_dir, slug_key, problems, progress):
    todo = [s for s in slugs if not (out_dir / cache_name(s)).exists()]
    log(f"{tool}: {len(todo)} to fetch ({len(slugs) - len(todo)} already cached)")
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        try:
            data = call_with_retry(client, tool, **{arg_name: chunk})
        except Exception as e:
            for s in chunk:
                problems.append(f"{s}: {tool} failed: {str(e)[:150]}")
                progress[s] = ("error", f"{tool}: {str(e)[:150]}")
            continue
        got = set()
        for item in data.get(result_key, []) if isinstance(data, dict) else []:
            slug = item.get(slug_key)
            if slug:
                write_json(out_dir / cache_name(slug), item)
                got.add(slug)
        for err in data.get("errors", []) if isinstance(data, dict) else []:
            problems.append(f"{err.get('slug')}: {tool} said {err.get('error')}")
            progress[err.get("slug")] = ("error", f"{tool}: {err.get('error')}")
        for s in chunk:
            if s not in got and s not in progress:
                problems.append(f"{s}: {tool} returned nothing")
                progress[s] = ("error", f"{tool}: returned nothing")
        log(f"  {tool}: {min(i + BATCH, len(todo))}/{len(todo)}")


# ---------------------------------------------------------------------------
# Loading into the database
# ---------------------------------------------------------------------------

def get_or_create(conn, table, key_cols, values, extra=None):
    where = " AND ".join(f"{c} IS ?" for c in key_cols)
    row = conn.execute(f"SELECT id FROM {table} WHERE {where}", [values[c] for c in key_cols]).fetchone()
    if row:
        return row[0]
    cols = list(key_cols) + list((extra or {}).keys())
    vals = [values[c] for c in key_cols] + list((extra or {}).values())
    cur = conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
    return cur.lastrowid


def instruments_text(entry):
    return ", ".join(i.get("name") or i.get("slug") or "" for i in entry.get("instruments") or [])


def is_tractorbeam(track):
    text = " ".join(track.get("annotations") or []) + " " + (track.get("note") or "")
    return bool(re.search(r"tractor\s*beam", text, re.I))


def set_order(label, fallback):
    m = re.match(r"([A-Z]+)(\d+)", label or "")
    if not m:
        return 500 + fallback
    base = {"S": 0, "E": 100}.get(m.group(1), 200)
    return base + int(m.group(2))


def load_show(conn, listing, setlist, details, problems, stats):
    slug = setlist["showSlug"]
    show_date = (details or {}).get("date") or setlist.get("showDate") or listing.get("date")
    venue = setlist.get("venue") or {}
    venue_id = get_or_create(conn, "venues", ["name", "city", "state"], {
        "name": venue.get("name") or listing.get("venueName"),
        "city": venue.get("city") or listing.get("venueCity"),
        "state": venue.get("state") or listing.get("venueState"),
    })
    sets_json = json.dumps(setlist.get("sets"), sort_keys=True, ensure_ascii=False)
    setlist_hash = hashlib.sha256(sets_json.encode("utf-8")).hexdigest()
    d = details or {}
    fields = {
        "api_id": d.get("id"),
        "date": show_date,
        "day_order": d.get("dayOrder"),
        "venue_id": venue_id,
        "count_for_stats": 0 if d.get("countForStats") is False else 1,
        "notes": d.get("notes"),
        "rating_avg": d.get("averageRating", listing.get("averageRating")),
        "ratings_count": d.get("ratingsCount", listing.get("ratingsCount")),
        "relisten_url": d.get("relistenUrl"),
        "setlist_hash": setlist_hash,
    }

    row = conn.execute("SELECT id, setlist_hash FROM shows WHERE slug = ?", (slug,)).fetchone()
    if row:
        show_id = row[0]
        conn.execute("DELETE FROM sets WHERE show_id = ?", (show_id,))  # cascades to tracks
        conn.execute("DELETE FROM sit_ins WHERE show_id = ?", (show_id,))
        conn.execute("DELETE FROM show_lineup WHERE show_id = ?", (show_id,))
        changed = row[1] != setlist_hash
        sets_sql = ", ".join(f"{k} = ?" for k in fields)
        extra = ", last_changed_at = ?" if changed else ""
        conn.execute(f"UPDATE shows SET {sets_sql}{extra} WHERE id = ?",
                     list(fields.values()) + ([now()] if changed else []) + [show_id])
    else:
        cols = ["slug", *fields, "first_seen_at", "last_changed_at"]
        cur = conn.execute(
            f"INSERT INTO shows ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [slug, *fields.values(), now(), now()])
        show_id = cur.lastrowid

    for m in setlist.get("lineup") or []:
        mid = get_or_create(conn, "musicians", ["slug"], {"slug": m["musicianSlug"]},
                            {"name": m.get("musicianName") or m["musicianSlug"]})
        conn.execute("INSERT OR REPLACE INTO show_lineup VALUES (?, ?, ?)",
                     (show_id, mid, instruments_text(m)))
        if m.get("knownFrom"):  # a guest listed for the whole show
            conn.execute("INSERT INTO sit_ins (show_id, perf_id, musician_id, instruments) VALUES (?, NULL, ?, ?)",
                         (show_id, mid, instruments_text(m)))

    prev_perf = None  # (perf_id, segue_out) of the previous track, across sets
    for n, st in enumerate(setlist.get("sets") or []):
        label = st.get("label") or f"X{n + 1}"
        tracks = st.get("tracks") or []
        tb = bool(tracks) and all(is_tractorbeam(t) for t in tracks)
        try:
            set_id = conn.execute(
                "INSERT INTO sets (show_id, label, set_order, set_type) VALUES (?, ?, ?, ?)",
                (show_id, label, set_order(label, n), "tractorbeam" if tb else "electric")).lastrowid
        except Exception as e:
            problems.append(f"{slug}: set {label} skipped ({e})")
            continue
        for t in sorted(tracks, key=lambda t: t.get("position") or 0):
            song_id = get_or_create(conn, "songs", ["slug"], {"slug": t.get("songSlug") or "unknown"},
                                    {"title": t.get("songTitle") or t.get("songSlug") or "Unknown"})
            segue = 1 if (t.get("segue") or "").strip() == ">" else 0
            try:
                perf_id = conn.execute(
                    """INSERT INTO performances (api_id, show_id, set_id, position, song_id, segue_out,
                       is_tractorbeam, note, annotations_json, duration_sec, all_timer)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (t.get("id"), show_id, set_id, t.get("position"), song_id, segue,
                     1 if is_tractorbeam(t) else 0, t.get("note"),
                     json.dumps(t.get("annotations") or [], ensure_ascii=False),
                     t.get("duration"), 1 if t.get("allTimer") else 0)).lastrowid
            except Exception as e:
                problems.append(f"{slug} {label} #{t.get('position')} {t.get('songTitle')}: track skipped ({e})")
                continue
            stats["performances"] += 1
            if prev_perf and prev_perf[1]:
                conn.execute("INSERT OR IGNORE INTO segues VALUES (?, ?)", (prev_perf[0], perf_id))
            prev_perf = (perf_id, segue)
            for flag in t.get("flags") or []:
                conn.execute("INSERT OR IGNORE INTO performance_flags VALUES (?, ?)", (perf_id, str(flag).upper()))
            for c in t.get("completes") or []:
                conn.execute(
                    "INSERT OR IGNORE INTO completions (completing_perf_id, target_show_slug, target_set, target_position) "
                    "VALUES (?, ?, ?, ?)", (perf_id, c.get("showSlug"), c.get("set"), c.get("position")))
            for g in t.get("trackMusicians") or []:
                if g.get("present") is False:
                    stats["absent_markers"] += 1
                    continue
                mid = get_or_create(conn, "musicians", ["slug"], {"slug": g["musicianSlug"]},
                                    {"name": g.get("musicianName") or g["musicianSlug"]})
                conn.execute("INSERT INTO sit_ins (show_id, perf_id, musician_id, instruments) VALUES (?, ?, ?, ?)",
                             (show_id, perf_id, mid, instruments_text(g)))
    return show_id


def resolve_completions(conn):
    conn.execute("""
        UPDATE completions SET completed_perf_id = (
            SELECT p.id FROM performances p
            JOIN sets s ON s.id = p.set_id
            JOIN shows sh ON sh.id = p.show_id
            WHERE sh.slug = completions.target_show_slug
              AND s.label = completions.target_set
              AND p.position = completions.target_position)
    """)
    return conn.execute("SELECT COUNT(*) FROM completions WHERE completed_perf_id IS NULL").fetchone()[0]


# ---------------------------------------------------------------------------
# Powered Down candidate scan
# ---------------------------------------------------------------------------

def scan_powered_down(shows):
    """shows: list of (date, slug, setlist, details). Returns text lines for the candidates file."""
    hits = defaultdict(list)  # (date, slug, where) -> reasons

    # Band members have knownFrom = null; guests have the band they're known from.
    def members(sl):
        return {m["musicianSlug"]: m for m in sl.get("lineup") or [] if not m.get("knownFrom")}

    def role(m):
        return ((m.get("defaultInstrument") or {}).get("slug")) or "?"

    def real_instruments(m):
        return {i.get("slug") for i in m.get("instruments") or []} - IGNORED_INSTRUMENTS

    # "Usual" members per year = band members in at least a quarter of that year's shows.
    # Each musician's usual instruments = ones used in at least 5% of their shows, all time.
    year_counts, year_member_counts = Counter(), defaultdict(Counter)
    plays, instr_counts = Counter(), defaultdict(Counter)
    for d, slug, sl, _ in shows:
        year_counts[d[:4]] += 1
        for s, m in members(sl).items():
            year_member_counts[d[:4]][s] += 1
            plays[s] += 1
            for i in real_instruments(m):
                instr_counts[s][i] += 1
    usual_by_year = {y: {s for s, n in c.items() if n >= 0.25 * year_counts[y]}
                     for y, c in year_member_counts.items()}
    usual_instr = {s: {i for i, n in c.items() if n >= 0.05 * plays[s]} for s, c in instr_counts.items()}
    roles = {}
    for d, slug, sl, _ in shows:
        for s, m in members(sl).items():
            roles.setdefault(s, role(m))

    for d, slug, sl, det in shows:
        year = d[:4]
        notes = (det or {}).get("notes") or ""
        if PD_WORDS.search(notes):
            hits[(d, slug, "whole show")].append(f"show notes mention '{PD_WORDS.search(notes).group(0)}': {notes[:140]}")
        if BILLING_WORDS.search(notes):
            hits[(d, slug, "whole show")].append(f"show notes suggest a different billing: {notes[:140]}")

        for st in sl.get("sets") or []:
            for t in st.get("tracks") or []:
                for source, text in [("track note", t.get("note") or "")] + \
                        [("annotation", a) for a in t.get("annotations") or []]:
                    m = PD_WORDS.search(text)
                    if m:
                        hits[(d, slug, st.get("label"))].append(
                            f"{source} on '{t.get('songTitle')}' mentions '{m.group(0)}': {text[:120]}")
                for inst in (i for g in t.get("trackMusicians") or [] for i in g.get("instruments") or []):
                    name = (inst.get("name") or "").lower()
                    if "acoustic" in name:
                        hits[(d, slug, st.get("label"))].append(
                            f"'{t.get('songTitle')}' lists an acoustic instrument ({name})")

        where = (d, slug, "whole show")
        mine = members(sl)
        if not sl.get("lineup"):
            hits[where].append("no lineup listed at all (can't check it)")
        else:
            usual = usual_by_year.get(year, set())
            covered = {i for m in mine.values() for i in real_instruments(m) | {role(m)}}
            for s in sorted(usual - set(mine)):
                if roles.get(s) not in covered:
                    hits[where].append(f"usual member {s} is absent and nobody else plays {roles.get(s)}")
            for s in sorted(set(mine) - usual):
                m = mine[s]
                hits[where].append(f"extra non-guest musician: {m.get('musicianName')} ({instruments_text(m)})")
            for s, m in mine.items():
                new = real_instruments(m) - usual_instr.get(s, set())
                if new:
                    hits[where].append(f"{m.get('musicianName')} played unusual instrument(s): {', '.join(sorted(new))}")

    lines = [
        "Powered Down candidates (made by scripts/backfill.py on " + date.today().isoformat() + ")",
        "Nothing here is used until you confirm it. Confirmed ones go in config.toml as",
        '  powered_down = ["YYYY-MM-DD S1", "YYYY-MM-DD"]   (a date alone = whole show)',
        "",
        "Format: date | set | slug | reason(s)",
        "",
    ]
    for (d, slug, where), reasons in sorted(hits.items()):
        lines.append(f"{d} | {where} | {slug}")
        for r in dict.fromkeys(reasons):
            lines.append(f"    - {r}")
    lines.append("")
    lines.append(f"{len(hits)} candidate(s).")
    return lines, len(hits)


# ---------------------------------------------------------------------------

def main():
    load_only = "--load" in sys.argv
    cfg = load_config()  # fail early if config.toml is broken
    conn = connect()
    init_db(conn)
    started = now()
    log_id = conn.execute("INSERT INTO sync_log (kind, started_at) VALUES ('backfill', ?)", (started,)).lastrowid
    conn.commit()
    problems = []
    progress = {}

    if not load_only:
        from mcp_client import MCPClient
        client = MCPClient()
        log("Step 1/4: listing shows for every year")
        fetch_years(client, problems)
    shows, placeholders = listed_shows()
    log(f"{len(shows)} real shows listed, {len(placeholders)} placeholder(s) skipped")
    for p in placeholders:
        progress[p] = ("skipped", "placeholder (no venue)")

    if not load_only:
        slugs = sorted(shows)
        log("Step 2/4: fetching setlists and show details")
        fetch_batches(client, slugs, "get_setlists", "showSlugs", "setlists", SETLISTS_DIR,
                      "showSlug", problems, progress)
        fetch_batches(client, slugs, "get_shows", "slugs", "shows", SHOWS_DIR, "slug", problems, progress)

    log("Step 3/4: loading the database")
    stats = Counter()
    loaded = []
    for slug in sorted(shows, key=lambda s: (shows[s]["date"], s)):
        sl_path = SETLISTS_DIR / cache_name(slug)
        if not sl_path.exists():
            progress.setdefault(slug, ("error", "no setlist in cache"))
            continue
        setlist = read_json(sl_path)
        det_path = SHOWS_DIR / cache_name(slug)
        details = read_json(det_path) if det_path.exists() else None
        if details is None:
            problems.append(f"{slug}: no show details (notes/countForStats missing); loaded anyway")
        load_show(conn, shows[slug], setlist, details, problems, stats)
        d = (details or {}).get("date") or setlist.get("showDate") or shows[slug]["date"]
        loaded.append((d, slug, setlist, details))
        progress[slug] = ("done", None)
        stats["shows"] += 1
    unresolved = resolve_completions(conn)
    for slug, (status, detail) in progress.items():
        conn.execute("INSERT OR REPLACE INTO backfill_progress VALUES (?, ?, ?, ?)", (slug, status, detail, now()))
    conn.commit()

    log("Parsing notes (set types, teases)")
    _, parse_problems = parse_notes.run(conn, cfg, verbose=False)
    problems += parse_problems

    log("Step 4/4: scanning for Powered Down candidates")
    lines, n_candidates = scan_powered_down(sorted(loaded))
    CANDIDATES_PATH.write_text("\n".join(lines), encoding="utf-8")

    # ---- summary ----
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    per_year = conn.execute("SELECT substr(date,1,4) y, COUNT(*) FROM shows GROUP BY y ORDER BY y").fetchall()
    summary = {
        "shows": q("SELECT COUNT(*) FROM shows"),
        "songs": q("SELECT COUNT(*) FROM songs"),
        "performances": q("SELECT COUNT(*) FROM performances"),
        "venues": q("SELECT COUNT(*) FROM venues"),
        "first_show": q("SELECT MIN(date) FROM shows"),
        "last_show": q("SELECT MAX(date) FROM shows"),
        "not_counted_for_stats": q("SELECT COUNT(*) FROM shows WHERE count_for_stats = 0"),
        "tractorbeam_sets": q("SELECT COUNT(*) FROM sets WHERE set_type = 'tractorbeam'"),
        "powered_down_sets": q("SELECT COUNT(*) FROM sets WHERE set_type = 'powered_down'"),
        "side_project_sets": q("SELECT COUNT(*) FROM sets WHERE set_type = 'side_project'"),
        "tractorbeam_versions": q("SELECT COUNT(*) FROM performances WHERE is_tractorbeam = 1"),
        "tractorbeam_portion_tracks": q("SELECT COUNT(*) FROM performances WHERE is_tractorbeam = 2"),
        "flags": q("SELECT COUNT(*) FROM performance_flags"),
        "completions": q("SELECT COUNT(*) FROM completions"),
        "completions_unresolved": unresolved,
        "sit_ins": q("SELECT COUNT(*) FROM sit_ins"),
        "whole_show_guests": q("SELECT COUNT(*) FROM sit_ins WHERE perf_id IS NULL"),
        "teases": q("SELECT COUNT(*) FROM teases"),
        "absent_markers": stats["absent_markers"],
        "placeholders_skipped": len(placeholders),
        "powered_down_candidates": n_candidates,
        "problems": len(problems),
    }
    conn.execute("UPDATE sync_log SET finished_at = ?, shows_added = ?, detail = ?, error = ? WHERE id = ?",
                 (now(), summary["shows"], json.dumps(summary),
                  "\n".join(problems)[:20000] or None, log_id))
    conn.commit()

    print("\n===== BACKFILL SUMMARY =====")
    for k, v in summary.items():
        print(f"{k:>26}: {v}")
    print("\nShows per year: " + ", ".join(f"{y}:{n}" for y, n in per_year))
    if problems:
        print(f"\nProblems ({len(problems)}):")
        for p in problems:
            print("  - " + p)
    print(f"\nPowered Down candidates written to {CANDIDATES_PATH}")


if __name__ == "__main__":
    main()
