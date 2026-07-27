#!/usr/bin/env python3
"""Prometheus exporter for the CUPS print proxy (stdlib only), with persistence.

Print counters are accumulated into a SQLite database that lives on the archive
hostPath (persistent across pod restarts / redeploys), so they are true
all-time counters -- not "since pod start". The exporter tails two ephemeral,
per-pod sources incrementally into the DB and detects pod restarts (the source
file's inode changes, or it shrank) to avoid double counting:

  * CUPS page_log      -> pages & jobs per user/printer/media/sides
  * archiver event log -> archived documents per user + forward failures

Archive size/file counts are read live from the archive tree (already durable).

Config (env):
  METRICS_PORT      listen port                (default 9101)
  CUPS_PAGE_LOG     cupsd page_log path        (default /var/log/cups/page_log)
  ARCHIVE_DIR       archive root               (default /archive)
  ARCHIVER_EVENTS   archiver event log         (default /var/log/cups/archiver-events.log)
  METRICS_DB        sqlite db path             (default ${ARCHIVE_DIR}/.print-metrics.db)

page_log format (see cupsd.conf): %p %u %j %P %C %{media} %{sides}
"""
import html
import os
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

METRICS_PORT = int(os.environ.get("METRICS_PORT", "9101"))
PAGE_LOG = os.environ.get("CUPS_PAGE_LOG", "/var/log/cups/page_log")
ARCHIVE_DIR = os.environ.get("ARCHIVE_DIR", "/archive")
EVENTS = os.environ.get("ARCHIVER_EVENTS", "/var/log/cups/archiver-events.log")
DB_PATH = os.environ.get("METRICS_DB", os.path.join(ARCHIVE_DIR, ".print-metrics.db"))
# internal relay queue: excluded from the user-facing print history
REAL_PRINTER = os.environ.get("REAL_PRINTER", "real-printer")
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", "500"))

_lock = threading.Lock()
_LABEL_RE = re.compile(r'([\\"\n])')


def esc(v):
    return _LABEL_RE.sub(lambda m: "\\n" if m.group(1) == "\n" else "\\" + m.group(1), str(v))


def labels(d):
    return ",".join(f'{k}="{esc(v)}"' for k, v in d.items())


def connect():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute("PRAGMA busy_timeout=10000")
    return c


def init_db():
    with connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS pages(
              user TEXT, printer TEXT, media TEXT, sides TEXT, n INTEGER,
              PRIMARY KEY(user, printer, media, sides));
            CREATE TABLE IF NOT EXISTS jobs(
              user TEXT, printer TEXT, n INTEGER, PRIMARY KEY(user, printer));
            CREATE TABLE IF NOT EXISTS archived(user TEXT PRIMARY KEY, n INTEGER);
            CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, n INTEGER);
            CREATE TABLE IF NOT EXISTS ingest(
              source TEXT PRIMARY KEY, offset INTEGER, inode INTEGER);
            CREATE TABLE IF NOT EXISTS history(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts INTEGER, user TEXT, host TEXT, name TEXT, printer TEXT, pages INTEGER);
            CREATE INDEX IF NOT EXISTS history_ts ON history(ts DESC);
            """
        )


def _new_lines(c, source):
    """Return complete lines appended to `source` since last ingest, handling
    pod restarts (inode change) and truncation."""
    try:
        st = os.stat(source)
    except OSError:
        return []
    row = c.execute("SELECT offset, inode FROM ingest WHERE source=?", (source,)).fetchone()
    off, inode = (row[0], row[1]) if row else (0, None)
    if inode != st.st_ino or st.st_size < off:  # new pod / rotated / truncated
        off = 0
    try:
        with open(source, "rb") as fh:
            fh.seek(off)
            data = fh.read()
    except OSError:
        return []
    nl = data.rfind(b"\n")
    if nl == -1:
        new_off = off
        lines = []
    else:
        lines = data[: nl + 1].decode("utf-8", "replace").splitlines()
        new_off = off + nl + 1
    c.execute(
        "INSERT INTO ingest(source, offset, inode) VALUES(?,?,?) "
        "ON CONFLICT(source) DO UPDATE SET offset=excluded.offset, inode=excluded.inode",
        (source, new_off, st.st_ino),
    )
    return lines


def _ingest_page_log(c):
    for line in _new_lines(c, PAGE_LOG):
        f = line.split()
        if len(f) < 5:
            continue
        printer, user, _job, pnum = f[0], f[1], f[2], f[3]
        try:
            count = int(f[4])
        except ValueError:
            continue
        # cups-pdf and the socket backend log one per-job "total" line (%P=total,
        # %C=total pages incl. copies). Count those; ignore per-page lines to
        # avoid double counting when a "total" line is also present.
        if pnum != "total":
            continue
        media = f[5] if len(f) > 5 and f[5] not in ("", "-") else "unknown"
        sides = f[6] if len(f) > 6 and f[6] not in ("", "-") else "unknown"
        host = f[7] if len(f) > 7 and f[7] not in ("", "-") else "unknown"
        try:
            ts = int(f[8]) if len(f) > 8 and f[8].isdigit() else int(time.time())
        except (ValueError, IndexError):
            ts = int(time.time())
        name = " ".join(f[9:]) if len(f) > 9 else "unknown"
        c.execute(
            "INSERT INTO pages(user, printer, media, sides, n) VALUES(?,?,?,?,?) "
            "ON CONFLICT(user, printer, media, sides) DO UPDATE SET n=n+excluded.n",
            (user, printer, media, sides, count),
        )
        c.execute(
            "INSERT INTO jobs(user, printer, n) VALUES(?,?,1) "
            "ON CONFLICT(user, printer) DO UPDATE SET n=n+1",
            (user, printer),
        )
        # per-job history (user-facing queues only; skip the internal relay)
        if printer != REAL_PRINTER:
            c.execute(
                "INSERT INTO history(ts, user, host, name, printer, pages) VALUES(?,?,?,?,?,?)",
                (ts, user, host, name, printer, count),
            )


def _ingest_events(c):
    for line in _new_lines(c, EVENTS):
        f = line.split()
        if len(f) < 3:
            continue
        _ts, user, status = f[0], f[1], f[2]
        c.execute(
            "INSERT INTO archived(user, n) VALUES(?,1) "
            "ON CONFLICT(user) DO UPDATE SET n=n+1",
            (user,),
        )
        if status != "ok":
            c.execute(
                "INSERT INTO counters(name, n) VALUES('forward_failures',1) "
                "ON CONFLICT(name) DO UPDATE SET n=n+1"
            )


def collect_archive():
    files, size = {}, {}
    try:
        entries = os.listdir(ARCHIVE_DIR)
    except OSError:
        return files, size
    for user in entries:
        udir = os.path.join(ARCHIVE_DIR, user)
        if not os.path.isdir(udir):
            continue
        nf = nb = 0
        for root, _dirs, fnames in os.walk(udir):
            for name in fnames:
                try:
                    nb += os.stat(os.path.join(root, name)).st_size
                    nf += 1
                except OSError:
                    pass
        files[user], size[user] = nf, nb
    return files, size


def render():
    out = []

    def metric(name, mtype, help_, samples):
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {mtype}")
        for lbls, val in samples:
            out.append(f"{name}{{{labels(lbls)}}} {val}" if lbls else f"{name} {val}")

    with connect() as c:
        _ingest_page_log(c)
        _ingest_events(c)
        pages = c.execute("SELECT user, printer, media, sides, n FROM pages ORDER BY 1,2,3,4").fetchall()
        jobs = c.execute("SELECT user, printer, n FROM jobs ORDER BY 1,2").fetchall()
        archived = c.execute("SELECT user, n FROM archived ORDER BY 1").fetchall()
        frow = c.execute("SELECT n FROM counters WHERE name='forward_failures'").fetchone()
    failures = frow[0] if frow else 0
    files, size = collect_archive()

    metric("cups_pages_printed_total", "counter",
           "Pages printed (page-sides x copies), all-time.",
           [({"user": u, "printer": p, "media": m, "sides": s}, n) for u, p, m, s, n in pages])
    metric("cups_jobs_total", "counter", "Print jobs, all-time.",
           [({"user": u, "printer": p}, n) for u, p, n in jobs])
    metric("cups_archived_documents_total", "counter",
           "Documents handled by the archiver, all-time.",
           [({"user": u}, n) for u, n in archived])
    metric("cups_forward_failures_total", "counter",
           "Jobs the archiver failed to forward to the physical printer, all-time.",
           [({}, failures)])
    metric("cups_archive_files", "gauge", "Files currently stored in the archive, per user.",
           [({"user": u}, n) for u, n in sorted(files.items())])
    metric("cups_archive_bytes", "gauge", "Bytes currently stored in the archive, per user.",
           [({"user": u}, n) for u, n in sorted(size.items())])
    metric("cups_exporter_up", "gauge", "Exporter is running.", [({}, 1)])
    return ("\n".join(out) + "\n").encode()


def history_rows():
    with connect() as c:
        _ingest_page_log(c)
        _ingest_events(c)
        return c.execute(
            "SELECT ts, user, host, name, printer, pages FROM history "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (HISTORY_LIMIT,),
        ).fetchall()


def render_history_json():
    rows = history_rows()
    import json
    items = [
        {"timestamp": ts, "user": u, "source_ip": h, "document": n, "printer": p, "pages": pg}
        for ts, u, h, n, p, pg in rows
    ]
    return json.dumps({"history": items}, ensure_ascii=False).encode()


def render_history_html():
    rows = history_rows()
    trs = []
    for ts, user, host, name, printer, pages in rows:
        lt = time.localtime(ts)
        trs.append(
            "<tr>"
            f"<td>{time.strftime('%Y-%m-%d', lt)}</td>"
            f"<td>{time.strftime('%H:%M:%S', lt)}</td>"
            f"<td>{html.escape(user or '')}</td>"
            f"<td>{html.escape(host or '')}</td>"
            f"<td class=doc>{html.escape(name or '')}</td>"
            f"<td class=num>{pages}</td>"
            f"<td>{html.escape(printer or '')}</td>"
            "</tr>"
        )
    body = (
        "<!doctype html><html lang=fr><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width, initial-scale=1'>"
        "<title>Historique d'impression</title><style>"
        "body{font-family:system-ui,sans-serif;margin:1.5rem;color:#222}"
        "h1{font-size:1.3rem}"
        "table{border-collapse:collapse;width:100%;font-size:.9rem}"
        "th,td{padding:.4rem .6rem;border-bottom:1px solid #ddd;text-align:left}"
        "th{background:#f3f4f6;position:sticky;top:0}"
        "tr:hover{background:#fafafa}.num{text-align:right}.doc{max-width:32rem;overflow:hidden;text-overflow:ellipsis}"
        "p.meta{color:#666;font-size:.8rem}"
        "</style></head><body>"
        f"<h1>Historique d'impression</h1>"
        f"<p class=meta>{len(rows)} dernières impressions (max {HISTORY_LIMIT}) &middot; "
        "<a href='/history.json'>JSON</a> &middot; <a href='/metrics'>metrics</a></p>"
        "<table><thead><tr><th>Date</th><th>Heure</th><th>Utilisateur</th>"
        "<th>IP source</th><th>Document</th><th>Pages</th><th>Imprimante</th></tr></thead>"
        f"<tbody>{''.join(trs)}</tbody></table></body></html>"
    )
    return body.encode()


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            with _lock:
                if path == "/metrics":
                    self._send(render(), "text/plain; version=0.0.4")
                elif path == "/history":
                    self._send(render_history_html(), "text/html; charset=utf-8")
                elif path == "/history.json":
                    self._send(render_history_json(), "application/json; charset=utf-8")
                else:
                    self.send_response(404)
                    self.end_headers()
        except Exception as exc:  # never crash
            body = f"# exporter error: {exc}\ncups_exporter_up 0\n".encode()
            try:
                self._send(body, "text/plain; charset=utf-8")
            except Exception:
                pass

    def log_message(self, *_a):
        pass


if __name__ == "__main__":
    init_db()
    ThreadingHTTPServer(("", METRICS_PORT), Handler).serve_forever()
