# scripts/python/init-tg-transfer-db.py
"""Reset tg-transfer DB + tmp directory to a clean state.

Runs INSIDE the tg-transfer-agent container (invoked by the sibling shell
wrapper), so paths are the container's Docker-volume paths.

Default behaviour: clear every data table but keep `config` (so the user
doesn't re-enter default_target_chat), reset auto-increment sequences, wipe
`/data/tg_transfer/tmp/`, VACUUM the DB file.

Flags (set via env vars from the shell wrapper so we don't have to re-parse):
  WIPE_CONFIG=1   also truncate the config table
  WIPE_TMP=0      skip tmp directory wipe
"""
import os
import shutil
import sqlite3
import sys

DB_PATH = "/data/tg_transfer/transfer.db"
TMP_DIR = "/data/tg_transfer/tmp"

# Every data table except `config`. Order matters only when FKs are enforced
# (SQLite doesn't by default) but is kept child-first for clarity.
DATA_TABLES = [
    "media_tags",
    "tags",
    "deferred_dedup",
    "pending_dedup",
    "job_messages",
    "jobs",
    "media",
]


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default) == "1"


def clear_db() -> None:
    if not os.path.exists(DB_PATH):
        print(f"[db] {DB_PATH} doesn't exist yet — agent will create it on "
              f"next start; nothing to clear.")
        return

    wipe_config = _flag("WIPE_CONFIG")
    before = os.path.getsize(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        for t in DATA_TABLES:
            try:
                n = cur.execute(f'DELETE FROM "{t}"').rowcount
                print(f"[db] cleared {t}: {n} rows")
            except sqlite3.OperationalError as e:
                # Table may not exist yet (fresh volume before agent ever ran).
                print(f"[db] skipped {t}: {e}")

        if wipe_config:
            try:
                n = cur.execute("DELETE FROM config").rowcount
                print(f"[db] cleared config: {n} rows")
            except sqlite3.OperationalError as e:
                print(f"[db] skipped config: {e}")
        else:
            rows = list(cur.execute("SELECT key, value FROM config"))
            print(f"[db] kept config: {len(rows)} row(s) {rows}")

        # Reset AUTOINCREMENT counters so new IDs start at 1 again.
        try:
            cur.execute("DELETE FROM sqlite_sequence")
            print("[db] reset sqlite_sequence")
        except sqlite3.OperationalError:
            # Table doesn't exist if nothing was ever autoincremented — fine.
            pass

        conn.commit()
    finally:
        conn.close()

    # VACUUM must run outside a transaction.
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()

    after = os.path.getsize(DB_PATH)
    print(f"[db] file size: {before} → {after} bytes "
          f"(-{before - after} reclaimed)")


def clear_tmp() -> None:
    if not _flag("WIPE_TMP", default="1"):
        print("[tmp] skipped (WIPE_TMP=0)")
        return
    if not os.path.isdir(TMP_DIR):
        print(f"[tmp] {TMP_DIR} missing — nothing to wipe")
        return

    removed = 0
    for name in os.listdir(TMP_DIR):
        p = os.path.join(TMP_DIR, name)
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            else:
                os.unlink(p)
            removed += 1
        except OSError as e:
            print(f"[tmp] failed to remove {p}: {e}", file=sys.stderr)
    print(f"[tmp] removed {removed} entries from {TMP_DIR}")


def main() -> int:
    print("=== init tg-transfer db ===")
    print(f"DB:      {DB_PATH}")
    print(f"Tmp:     {TMP_DIR}")
    print(f"Config:  {'WIPE' if _flag('WIPE_CONFIG') else 'KEEP'}")
    print(f"Tmp dir: {'WIPE' if _flag('WIPE_TMP', '1') else 'KEEP'}")
    print()
    clear_db()
    clear_tmp()
    print()
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
