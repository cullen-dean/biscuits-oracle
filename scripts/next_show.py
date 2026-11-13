"""Find the next scheduled show.

1. Read the "Next Show" box on the discobiscuits.net homepage (one request).
2. If that fails, fall back to data/upcoming.txt, which the user edits by hand:
       2026-12-14 | The Ardmore Music Hall | Ardmore, PA
   (one show per line; lines starting with # are ignored)

Returns a dict: {date, venue, location, source, notice}
`notice` is a message for the website when the fallback was used (else None).
"""

import html
import re
from datetime import date, datetime
from pathlib import Path

import requests

HOME_URL = "https://discobiscuits.net/"
USER_AGENT = "biscuits-oracle/0.1 (personal non-commercial fan stats)"
UPCOMING_FILE = Path(__file__).parent.parent / "data" / "upcoming.txt"


def _text(fragment):
    """Strip HTML tags/comments and tidy whitespace."""
    fragment = re.sub(r"<!--.*?-->", "", fragment, flags=re.S)
    fragment = re.sub(r"<[^>]+>", "\n", fragment)
    lines = [html.unescape(l).strip() for l in fragment.splitlines()]
    return [l for l in lines if l]


def from_homepage(page_html):
    """Parse the Next Show box. Raises ValueError if the layout changed."""
    m = re.search(r">\s*Next Show\s*</h3>", page_html, flags=re.I)
    if not m:
        raise ValueError("'Next Show' heading not found on homepage")
    parts = _text(page_html[m.end(): m.end() + 1500])
    if len(parts) < 2:
        raise ValueError("Next Show box is missing date or venue")
    show_date = datetime.strptime(parts[0], "%b %d, %Y").date()
    venue, _, location = parts[1].partition("•")
    return {"date": show_date, "venue": venue.strip(), "location": location.strip()}


def from_upcoming_file(today):
    if not UPCOMING_FILE.exists():
        return None
    for line in UPCOMING_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        bits = [b.strip() for b in line.split("|")]
        d = date.fromisoformat(bits[0])
        if d >= today:
            return {"date": d, "venue": bits[1] if len(bits) > 1 else "",
                    "location": bits[2] if len(bits) > 2 else ""}
    return None


def find_next_show(today=None):
    today = today or date.today()
    try:
        resp = requests.get(HOME_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
        resp.raise_for_status()
        show = from_homepage(resp.text)
        return {**show, "source": "discobiscuits.net homepage", "notice": None}
    except Exception as err:  # any failure -> fall back to the hand-edited file
        reason = str(err)
    show = from_upcoming_file(today)
    notice = (f"Couldn't read the next show from discobiscuits.net ({reason}). "
              f"Using data/upcoming.txt instead.")
    if show:
        return {**show, "source": "data/upcoming.txt", "notice": notice}
    return {"date": None, "venue": None, "location": None, "source": None,
            "notice": notice + " That file has no upcoming shows either, so add one there."}


if __name__ == "__main__":
    print(find_next_show())
