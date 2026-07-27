#!/usr/bin/env bash
#
# Root-run archiver daemon (started by supervisord).
#
# cups-pdf renders each job, unprivileged, to ${SPOOL}/<user>/<title>.pdf. The
# privileged steps -- writing into the root-owned /archive dataset and submitting
# to the internal "real-printer" queue (op-policy internalonly => @SYSTEM only) --
# cannot run from cups-pdf's PostProcessing hook, which executes as the printing
# user. So we poll the spool as root instead (polling, not inotify: osmium's
# inotify instances are frequently exhausted).
#
# For every PDF that has been stable for a moment we:
#   - archive it to ${ARCHIVE_DIR}/<user>/<year>/<month>/<day>/<HH-MM-SS>_<title>.pdf
#   - group/permission it so Samba can expose it read-only to ${ARCHIVE_GROUP}
#   - forward it to ${REAL_PRINTER} (the physical printer)
#   - remove the spooled copy
set -uo pipefail

ARCHIVE_DIR="${ARCHIVE_DIR:-/archive}"
REAL_PRINTER="${REAL_PRINTER:-real-printer}"
# optional group given read access to the archive (empty -> rely on /archive setgid)
ARCHIVE_GROUP="${ARCHIVE_GROUP:-}"
SPOOL="${CUPS_PDF_SPOOL:-/var/spool/cups-pdf}"
# append-only event log consumed by cups-exporter.py: "<epoch> <user> <ok|forward_failed>"
EVENTS="${ARCHIVER_EVENTS:-/var/log/cups/archiver-events.log}"
POLL_INTERVAL="${ARCHIVER_POLL_INTERVAL:-2}"
# only touch files whose mtime is at least this many seconds old (avoid grabbing a
# PDF still being written by cups-pdf)
STABLE_AGE="${ARCHIVER_STABLE_AGE:-2}"

log() { echo "archiver: $*" >&2; }

log "watching ${SPOOL} (every ${POLL_INTERVAL}s); archive=${ARCHIVE_DIR} group=${ARCHIVE_GROUP:-<none>} printer=${REAL_PRINTER}"

process() {
  local pdf="$1" user title day_path ts dest_dir dest
  user=$(basename "$(dirname "${pdf}")")
  # cups-pdf's own working dir, never a user
  [ "${user}" = "SPOOL" ] && return 0

  title=$(basename "${pdf}" .pdf | tr -cd '[:alnum:]._ -' | tr ' ' '_')
  [ -n "${title}" ] || title="print"

  day_path=$(date +%Y/%m/%d)
  ts=$(date +%H-%M-%S)
  dest_dir="${ARCHIVE_DIR}/${user}/${day_path}"
  dest="${dest_dir}/${ts}_${title}.pdf"

  if ! mkdir -p "${dest_dir}"; then
    log "mkdir failed: ${dest_dir} (leaving ${pdf} in spool)"
    return 1
  fi
  if ! cp "${pdf}" "${dest}"; then
    log "cp failed: ${pdf} -> ${dest} (leaving ${pdf} in spool)"
    return 1
  fi

  # permissions so Samba can serve the archive read-only to the configured group
  if [ -n "${ARCHIVE_GROUP}" ]; then
    chgrp -R "${ARCHIVE_GROUP}" "${ARCHIVE_DIR}/${user}" 2>/dev/null || true
  fi
  find "${ARCHIVE_DIR}/${user}" -type d -exec chmod 2750 {} + 2>/dev/null || true
  chmod 0640 "${dest}" 2>/dev/null || true

  # forward to the physical printer (root == @SYSTEM, allowed by internalonly)
  if lp -d "${REAL_PRINTER}" "${pdf}" >/dev/null 2>&1; then
    log "archived + forwarded: user=${user} -> ${dest}"
    echo "$(date +%s) ${user} ok" >> "${EVENTS}" 2>/dev/null || true
  else
    log "archived but FORWARD FAILED to ${REAL_PRINTER}: user=${user} (kept ${dest})"
    echo "$(date +%s) ${user} forward_failed" >> "${EVENTS}" 2>/dev/null || true
  fi

  rm -f "${pdf}" 2>/dev/null || true
  # tidy empty user spool dir
  rmdir "$(dirname "${pdf}")" 2>/dev/null || true
}

while true; do
  now=$(date +%s)
  while IFS= read -r pdf; do
    [ -f "${pdf}" ] || continue
    mt=$(stat -c %Y "${pdf}" 2>/dev/null || echo "${now}")
    [ $((now - mt)) -ge "${STABLE_AGE}" ] || continue
    process "${pdf}"
  done < <(find "${SPOOL}" -mindepth 2 -type f -name '*.pdf' ! -path "${SPOOL}/SPOOL/*" 2>/dev/null)
  sleep "${POLL_INTERVAL}"
done
