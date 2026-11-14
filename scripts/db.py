"""Open the SQLite database and load settings.

    python scripts/db.py      -> creates data/biscuits.db (if needed) and lists its tables
"""

import sqlite3
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "data" / "biscuits.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"
CONFIG_PATH = ROOT / "config.toml"


def connect(path=DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn):
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def load_config():
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


if __name__ == "__main__":
    cfg = load_config()
    conn = connect()
    init_db(conn)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print(f"Database: {DB_PATH}")
    print(f"{len(tables)} tables: {', '.join(tables)}")
    print(f"Config loaded: {len(cfg)} top-level settings, eras = {[e['name'] for e in cfg['eras']]}")
