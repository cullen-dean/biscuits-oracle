"""Step 6: turn free-text notes and annotations into structured data.

    python scripts/parse_notes.py     -> re-parse everything already in data/biscuits.db

Works only on what's in the database (no API calls), so it's safe to re-run any time.
backfill.py and update.py call run() after loading shows.

What it does:
  1. Set types: Tractorbeam from track annotations and show notes, then the
     hand-kept powered_down / side_project lists in config.toml (these always win).
  2. Teases, samples, jams, fakeouts, intros and lyric quotes from track annotations.
  3. Writes data/tractorbeam_check.txt: shows where the Tractorbeam notes were unclear.
"""

import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db import ROOT, connect, init_db, load_config  # noqa: E402

TB_CHECK_PATH = ROOT / "data" / "tractorbeam_check.txt"
TB = re.compile(r"tractor\s*beam", re.I)

# ---------------------------------------------------------------------------
# Tractorbeam from show notes
# ---------------------------------------------------------------------------

ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4}
ORDINAL = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3}
SET_REF = re.compile(r"\bset\s+(iv|iii|ii|i|\d)\b|\b(first|second|third|1st|2nd|3rd)\s+set\b", re.I)
NOT_A_PERFORMANCE = re.compile(r"soundcheck|workshop", re.I)
STARTING_WITH = re.compile(r"starting with\s+[‘'\"“]([^’'\"”]+)[’'\"”]", re.I)
# Track annotation like "Switched to Tractorbeam (‘Orch Theme’ through ‘Hymn’)"
TB_RANGE = re.compile(r"[‘'“]([^’'”]+)[’'”]\s+through\s+[‘'“]([^’'”]+)[’'”]", re.I)


def tractorbeam_range(tracks):
    """Find a Tractorbeam song range in the track annotations. Returns (track keys, description) or None."""
    for t in tracks:
        for text in json.loads(t[3] or "[]"):
            m = TB.search(text) and TB_RANGE.search(text)
            if not m:
                continue
            first, last = m.group(1).strip().lower(), m.group(2).strip().lower()
            titles = [x[2].lower() for x in tracks]
            if first in titles and last in titles[titles.index(first):]:
                i = titles.index(first)
                j = titles.index(last, i)
                return {(x[0], x[1]) for x in tracks[i:j + 1]}, \
                    f"'{tracks[i][2]}' ({tracks[i][0]} #{tracks[i][1]}) through '{tracks[j][2]}' ({tracks[j][0]} #{tracks[j][1]})"
    return None


def note_segments(notes):
    return [s.strip() for s in re.split(r"<br\s*/?>|\n", notes or "") if s.strip()]


def tractorbeam_from_notes(notes, set_labels, tracks):
    """Decide which parts of a show the notes say were Tractorbeam.

    set_labels: the show's set labels in order, e.g. ["S1", "S2", "E1"]
    tracks: list of (set_label, position, song_title, annotations_json) in show order
    Returns (sets_to_tag, tracks_to_tag, unclear_reasons).

    Priority: a set named in the notes > a "'A' through 'B'" range in a track annotation
    > "starting with 'X'" in the notes > the whole show.
    """
    sets_to_tag, tracks_to_tag, unclear = set(), set(), []
    segments = note_segments(notes)
    tb_segments = [s for s in segments if TB.search(s)]
    if not tb_segments:
        return sets_to_tag, tracks_to_tag, unclear
    if not set_labels:
        unclear.append("no setlist in the API, so nothing to tag")
        return sets_to_tag, tracks_to_tag, unclear
    song_range = tractorbeam_range(tracks)

    for seg in tb_segments:
        if NOT_A_PERFORMANCE.search(seg):
            unclear.append(f"mentions Tractorbeam but not as a performance, so NOT tagged: \"{seg[:100]}\"")
            continue

        # Named sets: "Set I was ...", "Set 3 performed ...", "First set played as ..."
        named = set()
        for m in SET_REF.finditer(seg):
            word = (m.group(1) or m.group(2)).lower()
            n = ROMAN.get(word) or ORDINAL.get(word) or (int(word) if word.isdigit() else None)
            if n:
                named.add(f"S{n}")
        if named:
            missing = named - set(set_labels)
            if missing:
                unclear.append(f"notes name {', '.join(sorted(missing))} but the API has no such set ({seg[:80]})")
            sets_to_tag |= named & set(set_labels)
            if re.search(r"perfume", seg, re.I):
                unclear.append(f"tagged {', '.join(sorted(named))} as Tractorbeam, but it was "
                               f"'Tractorbeam vs. The Perfume': \"{seg[:100]}\"")
            continue

        # A track annotation gives the exact songs: "Switched to Tractorbeam ('Orch Theme' through 'Hymn')"
        if song_range:
            if not song_range[0] <= tracks_to_tag:
                tracks_to_tag |= song_range[0]
                unclear.append(f"split show: tagged only {song_range[1]} as Tractorbeam "
                               f"(from a track annotation); the rest stays a normal set")
            continue

        # "... performed the 2nd half as Tractorbeam starting with 'Quad D'"
        m = STARTING_WITH.search(seg)
        if m:
            start = m.group(1).strip().lower()
            hit = next((i for i, t in enumerate(tracks) if t[2].lower() == start), None)
            if hit is not None:
                tracks_to_tag |= {(t[0], t[1]) for t in tracks[hit:]}
                unclear.append(f"Tractorbeam was only part of a set: tagged '{tracks[hit][2]}' "
                               f"({tracks[hit][0]} #{tracks[hit][1]}) and every song after it, set left as normal")
            else:
                sets_to_tag |= set(set_labels)
                unclear.append(f"notes say Tractorbeam started with '{m.group(1)}' but that song isn't "
                               f"in the setlist, so tagged the whole show")
            continue

        # No set named: whole show (your rule).
        sets_to_tag |= set(set_labels)
        others = [s for s in segments if s is not seg and re.search(r"\btdb\b|disco biscuits", s, re.I)
                  and re.search(r"\d{1,2}(:\d\d)?\s*-?\s*\d{1,2}(:\d\d)?\s*[ap]", s, re.I)]
        if others:
            unclear.append(f"tagged the whole show, but the notes list a separate tDB slot too "
                           f"(\"{others[0][:60]}\" / \"{seg[:60]}\"), so maybe only part was Tractorbeam")
        elif re.search(r"festival|\bacts include", notes or "", re.I) is None and len(set_labels) > 2:
            unclear.append(f"tagged the whole show ({len(set_labels)} sets) from \"{seg[:60]}\"")
    return sets_to_tag, tracks_to_tag, unclear


# ---------------------------------------------------------------------------
# Teases, samples, jams ... from track annotations
# ---------------------------------------------------------------------------

QUOTED = re.compile(
    r"[‘“]([^’”]+)[’”]"                 # curly quotes (the usual style)
    r"|(?<![A-Za-z])'([^']+)'(?![A-Za-z])"   # 'straight quotes'
    r"|\"([^\"]+)\"")
ARTIST_AFTER = re.compile(r"\s*\(((?:[^()]|\([^()]*\))*)\)")
KIND_WORD = re.compile(
    r"\b(?:vocal\s+)?(teases?|samples?|jam|fake[- ]?out|intro|lyrics|chant|(?<!on )vocals)\b", re.I)


def kind_of(word):
    w = re.sub(r"[- ]", "", word.lower())
    if w.startswith("tease"):
        return "tease"
    if w.startswith("sample"):
        return "sample"
    if w in ("lyrics", "chant", "vocals"):
        return "lyrics"
    return w  # jam, fakeout, intro
NOT_AN_ARTIST = re.compile(r"^(ftp|debut|first|1st|\d)", re.I)


def parse_annotation(text):
    """'with ‘Jam On It’ (Newcleus) teases' -> [("tease", "Jam On It", "Newcleus")]"""
    found = []
    quotes = []
    for m in QUOTED.finditer(text):
        title = next(g for g in m.groups() if g is not None).strip()
        artist = None
        a = ARTIST_AFTER.match(text, m.end())
        end = m.end()
        if a and not NOT_AN_ARTIST.match(a.group(1).strip()):
            artist = a.group(1).strip()
            end = a.end()
        quotes.append((m.start(), end, title, artist))
    if not quotes:
        return found
    # Blank out titles and artists so a title like 'Jam On It' isn't read as the word "jam".
    masked = list(text)
    for start, end, _, _ in quotes:
        masked[start:end] = " " * (end - start)
    masked = "".join(masked)
    # Each kind word claims the quoted titles that come before it (back to the previous kind word).
    last = 0
    for k in KIND_WORD.finditer(masked):
        kind = kind_of(k.group(1))
        for start, end, title, artist in quotes:
            if last <= start and end <= k.start():
                found.append((kind, title, artist))
        last = k.end()
    return found


def norm_title(s):
    s = unicodedata.normalize("NFKD", s).replace("’", "'").replace("‘", "'")
    return re.sub(r"[^a-z0-9]+", "", s.lower())


# ---------------------------------------------------------------------------

def run(conn, cfg, verbose=True):
    problems = []

    # 1a. Reset track flags from annotations first, so tags from an earlier run don't stick.
    conn.execute("""UPDATE performances SET is_tractorbeam =
        CASE WHEN annotations_json LIKE '%ractor%eam%' OR note LIKE '%ractor%eam%' THEN 1 ELSE 0 END""")
    # Then automatic: a set where every track is marked Tractorbeam.
    conn.execute("""
        UPDATE sets SET set_type = CASE WHEN
            (SELECT COUNT(*) FROM performances p WHERE p.set_id = sets.id) > 0 AND
            (SELECT COUNT(*) FROM performances p WHERE p.set_id = sets.id AND p.is_tractorbeam = 0) = 0
          THEN 'tractorbeam' ELSE 'electric' END""")

    # 1b. From show notes.
    unclear_lines = []
    tb_shows = conn.execute(
        "SELECT id, date, slug, notes FROM shows WHERE notes LIKE '%ractor%eam%' ORDER BY date").fetchall()
    for show in tb_shows:
        sets = conn.execute("SELECT id, label FROM sets WHERE show_id = ? ORDER BY set_order",
                            (show["id"],)).fetchall()
        tracks = conn.execute("""
            SELECT s.label, p.position, so.title, p.annotations_json FROM performances p
            JOIN sets s ON s.id = p.set_id JOIN songs so ON so.id = p.song_id
            WHERE p.show_id = ? ORDER BY s.set_order, p.position""", (show["id"],)).fetchall()
        tag_sets, tag_tracks, unclear = tractorbeam_from_notes(
            show["notes"], [s["label"] for s in sets], [tuple(t) for t in tracks])
        for s in sets:
            if s["label"] in tag_sets:
                conn.execute("UPDATE sets SET set_type = 'tractorbeam' WHERE id = ?", (s["id"],))
                conn.execute("UPDATE performances SET is_tractorbeam = 2 WHERE set_id = ?", (s["id"],))
        for label, pos in tag_tracks:
            conn.execute("""UPDATE performances SET is_tractorbeam = 2 WHERE show_id = ? AND position = ?
                            AND set_id = (SELECT id FROM sets WHERE show_id = ? AND label = ?)""",
                         (show["id"], pos, show["id"], label))
        if unclear:
            unclear_lines.append(f"{show['date']} | {show['slug']}")
            unclear_lines += [f"    - {u}" for u in unclear]
            unclear_lines.append(f"    notes: {' / '.join(note_segments(show['notes']))[:300]}")

    # 1c. Hand-kept lists win over everything.
    for set_type in ("powered_down", "side_project"):
        for entry in cfg.get(set_type, []):
            parts = entry.split()
            day, label = parts[0], (parts[1] if len(parts) > 1 else None)
            show_ids = [r[0] for r in conn.execute("SELECT id FROM shows WHERE date = ?", (day,))]
            if not show_ids:
                problems.append(f"config {set_type} '{entry}': no show on that date")
            if len(show_ids) > 1:
                problems.append(f"config {set_type} '{entry}': {len(show_ids)} shows that day, tagged all")
            for sid in show_ids:
                sql = "UPDATE sets SET set_type = ? WHERE show_id = ?" + (" AND label = ?" if label else "")
                if conn.execute(sql, [set_type, sid] + ([label] if label else [])).rowcount == 0:
                    problems.append(f"config {set_type} '{entry}': no set {label} in that show")

    # 2. Teases etc.
    conn.execute("DELETE FROM teases")
    songs = {norm_title(r["title"]): r["id"] for r in conn.execute("SELECT id, title FROM songs")}
    n_teases = n_linked = n_annotations = n_unparsed = 0
    for p in conn.execute("SELECT id, annotations_json FROM performances WHERE annotations_json != '[]'").fetchall():
        for text in json.loads(p["annotations_json"]):
            n_annotations += 1
            items = parse_annotation(text)
            if not items and QUOTED.search(text):
                n_unparsed += 1
            for kind, title, artist in items:
                song_id = songs.get(norm_title(title))
                conn.execute("INSERT INTO teases (perf_id, kind, title, artist, song_id) VALUES (?, ?, ?, ?, ?)",
                             (p["id"], kind, title, artist, song_id))
                n_teases += 1
                n_linked += song_id is not None
    conn.commit()

    # 3. The check file.
    header = ["Tractorbeam: shows to check (made by scripts/parse_notes.py)",
              "Rule: a set named in the notes -> only that set; no set named -> whole show.",
              "Each entry says what was tagged and why it looked unclear.", ""]
    TB_CHECK_PATH.write_text("\n".join(header + (unclear_lines or ["(none)"])) + "\n", encoding="utf-8")

    summary = {
        "tractorbeam_sets": conn.execute("SELECT COUNT(*) FROM sets WHERE set_type='tractorbeam'").fetchone()[0],
        "tractorbeam_shows_from_notes": len(tb_shows),
        "tractorbeam_unclear_shows": sum(1 for line in unclear_lines if not line.startswith("    ")),
        "powered_down_sets": conn.execute("SELECT COUNT(*) FROM sets WHERE set_type='powered_down'").fetchone()[0],
        "side_project_sets": conn.execute("SELECT COUNT(*) FROM sets WHERE set_type='side_project'").fetchone()[0],
        "annotations": n_annotations,
        "teases_found": n_teases,
        "teases_linked_to_known_songs": n_linked,
        "annotations_with_quotes_not_parsed": n_unparsed,
    }
    if verbose:
        for k, v in summary.items():
            print(f"{k:>36}: {v}")
        for p in problems:
            print("  problem: " + p)
    return summary, problems


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    conn = connect()
    init_db(conn)
    run(conn, load_config())
    print(f"\nTractorbeam check list: {TB_CHECK_PATH}")
