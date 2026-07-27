#!/usr/bin/env python3
"""Tiny Prometheus exporter for the CUPS print proxy (stdlib only).

On each scrape it recomputes everything from scratch (cheap for a home printer),
so there is no state to persist -- counters are naturally "since pod start":

  * CUPS page_log      -> pages & jobs per user/printer/media/sides
  * /archive tree      -> archived bytes & file count per user
  * archiver event log -> archived documents & forward failures

Config (env):
  METRICS_PORT      listen port                (default 9101)
  CUPS_PAGE_LOG     cupsd page_log path        (default /var/log/cups/page_log)
  ARCHIVE_DIR       archive root               (default /archive)
  ARCHIVER_EVENTS   archiver event log         (default /var/log/cups/archiver-events.log)

page_log is expected in the format configured in cupsd.conf:
  %p %u %j %P %C %{media} %{sides}
  printer user job-id page-number copies media sides
"""
import os
import re
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

METRICS_PORT = int(os.environ.get("METRICS_PORT", "9101"))
PAGE_LOG = os.environ.get("CUPS_PAGE_LOG", "/var/log/cups/page_log")
ARCHIVE_DIR = os.environ.get("ARCHIVE_DIR", "/archive")
EVENTS = os.environ.get("ARCHIVER_EVENTS", "/var/log/cups/archiver-events.log")

_LABEL_RE = re.compile(r'([\\"\n])')


def esc(v):
    return _LABEL_RE.sub(lambda m: "\\n" if m.group(1) == "\n" else "\\" + m.group(1), str(v))


def labels(d):
    return ",".join(f'{k}="{esc(v)}"' for k, v in d.items())


def collect_page_log():
    """Return (pages{(user,printer,media,sides)}, jobs{(user,printer)}).

    CUPS logs either one line per page (numeric page number, %C = copies of that
    page) or a single per-job summary line (page number "total", %C = the job's
    total page count). cups-pdf uses the "total" form. We group by (printer, job)
    and prefer the authoritative "total" when present, else sum the per-page lines.
    """
    # (printer, job) -> {"user","media","sides","per_page","total"}
    seen = {}
    try:
        with open(PAGE_LOG, "r", errors="replace") as fh:
            for line in fh:
                f = line.split()
                if len(f) < 5:
                    continue
                printer, user, job, pnum = f[0], f[1], f[2], f[3]
                try:
                    count = int(f[4])
                except ValueError:
                    continue
                media = f[5] if len(f) > 5 and f[5] not in ("", "-") else "unknown"
                sides = f[6] if len(f) > 6 and f[6] not in ("", "-") else "unknown"
                e = seen.setdefault((printer, job), {
                    "user": user, "media": "unknown", "sides": "unknown",
                    "per_page": 0, "total": None})
                if user not in ("", "-"):
                    e["user"] = user
                if media != "unknown":
                    e["media"] = media
                if sides != "unknown":
                    e["sides"] = sides
                if pnum == "total":
                    e["total"] = count
                elif pnum.isdigit():
                    e["per_page"] += max(count, 1)

    except FileNotFoundError:
        pass

    pages = defaultdict(int)
    jobs = defaultdict(int)
    for (printer, _job), e in seen.items():
        n = e["total"] if e["total"] is not None else e["per_page"]
        if n <= 0:
            continue
        pages[(e["user"], printer, e["media"], e["sides"])] += n
        jobs[(e["user"], printer)] += 1
    return pages, jobs


def collect_archive():
    """Return (files{user}, bytes{user})."""
    files = defaultdict(int)
    size = defaultdict(int)
    try:
        for user in os.listdir(ARCHIVE_DIR):
            udir = os.path.join(ARCHIVE_DIR, user)
            if not os.path.isdir(udir):
                continue
            for root, _dirs, fnames in os.walk(udir):
                for name in fnames:
                    try:
                        st = os.stat(os.path.join(root, name))
                    except OSError:
                        continue
                    files[user] += 1
                    size[user] += st.st_size
    except FileNotFoundError:
        pass
    return files, size


def collect_events():
    """Return (archived{user}, forward_failures_total)."""
    archived = defaultdict(int)
    failures = 0
    try:
        with open(EVENTS, "r", errors="replace") as fh:
            for line in fh:
                f = line.split()
                if len(f) < 3:
                    continue
                _ts, user, status = f[0], f[1], f[2]
                archived[user] += 1
                if status != "ok":
                    failures += 1
    except FileNotFoundError:
        pass
    return archived, failures


def render():
    out = []

    def metric(name, mtype, help_, samples):
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {mtype}")
        for lbls, val in samples:
            out.append(f"{name}{{{labels(lbls)}}} {val}" if lbls else f"{name} {val}")

    pages, jobs = collect_page_log()
    files, size = collect_archive()
    archived, failures = collect_events()

    metric("cups_pages_printed_total", "counter",
           "Pages printed (page-sides x copies) since pod start.",
           [({"user": u, "printer": p, "media": m, "sides": s}, v)
            for (u, p, m, s), v in sorted(pages.items())])

    metric("cups_jobs_total", "counter",
           "Print jobs since pod start.",
           [({"user": u, "printer": p}, v) for (u, p), v in sorted(jobs.items())])

    metric("cups_archived_documents_total", "counter",
           "Documents handled by the archiver since pod start.",
           [({"user": u}, v) for u, v in sorted(archived.items())])

    metric("cups_forward_failures_total", "counter",
           "Jobs the archiver failed to forward to the physical printer.",
           [({}, failures)])

    metric("cups_archive_files", "gauge",
           "Files currently stored in the archive, per user.",
           [({"user": u}, v) for u, v in sorted(files.items())])

    metric("cups_archive_bytes", "gauge",
           "Bytes currently stored in the archive, per user.",
           [({"user": u}, v) for u, v in sorted(size.items())])

    metric("cups_exporter_up", "gauge", "Exporter is running.", [({}, 1)])

    return ("\n".join(out) + "\n").encode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        try:
            body = render()
        except Exception as exc:  # never crash the scrape
            body = f"# exporter error: {exc}\ncups_exporter_up 0\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        pass  # quiet


if __name__ == "__main__":
    ThreadingHTTPServer(("", METRICS_PORT), Handler).serve_forever()
