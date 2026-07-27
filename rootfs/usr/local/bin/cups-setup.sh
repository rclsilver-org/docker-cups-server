#!/usr/bin/env bash
#
# One-shot (run by supervisord after cupsd): declares the two CUPS queues.
#   real-printer  : PPD -> socket://<printer>:9100, printable only by the local
#                   system (the forward.sh hook). Never exposed to clients.
#   family-printer: cups-pdf backend (archives + forwards via PostProcessing),
#                   default queue, printing restricted to @${PRINT_GROUP}.
set -uo pipefail

# Site-specific values MUST be provided at runtime (env).
PRINTER_IP="${PRINTER_IP:?PRINTER_IP env var is required (physical printer IP)}"
PRINTER_PORT="${PRINTER_PORT:-9100}"
PPD="${PRINTER_PPD:?PRINTER_PPD env var is required (path to the printer PPD)}"

# wait until cupsd is actually accepting requests
for _ in $(seq 1 90); do
  if lpstat -r 2>/dev/null | grep -qi "is running"; then break; fi
  sleep 1
done

# cupsd can still briefly return transient errors right after startup -> retry.
retry() {
  local n=0
  until "$@"; do
    n=$((n + 1))
    if [ "$n" -ge 15 ]; then
      echo "cups-setup: command failed after ${n} attempts: $*" >&2
      return 1
    fi
    sleep 2
  done
}

# physical printer (internal only)
retry lpadmin -p real-printer -E \
  -v "socket://${PRINTER_IP}:${PRINTER_PORT}" \
  -P "${PPD}" \
  -o printer-op-policy=internalonly \
  -D "${PRINTER_DESC:-Physical printer}"
retry cupsaccept real-printer
retry cupsenable real-printer

# family-facing queue (cups-pdf). Use the CUPS-PDF PPD if present, else raw.
CUPS_PDF_PPD=$(ls /usr/share/ppd/cups-pdf/*.ppd /usr/share/cups/model/CUPS-PDF*.ppd 2>/dev/null | head -1 || true)
# Force software copies: cups-pdf renders a single copy otherwise, so multi-copy
# jobs would print only once on the physical printer. cupsManualCopies makes CUPS
# expand copies upstream, so the rendered PDF already contains every copy.
if [ -n "${CUPS_PDF_PPD}" ]; then
  PATCHED_PPD=/etc/cups/family-printer.ppd
  cp "${CUPS_PDF_PPD}" "${PATCHED_PPD}"
  grep -qi '^\*cupsManualCopies:' "${PATCHED_PPD}" || echo '*cupsManualCopies: True' >> "${PATCHED_PPD}"
  CUPS_PDF_PPD="${PATCHED_PPD}"
fi
retry lpadmin -p family-printer -E \
  -v cups-pdf:/ \
  ${CUPS_PDF_PPD:+-P "${CUPS_PDF_PPD}"} \
  -o printer-op-policy=authprint \
  -D "Imprimante famille" \
  -L "Maison"
retry cupsaccept family-printer
retry cupsenable family-printer
lpadmin -d family-printer

echo "cups-setup: done (real-printer -> socket://${PRINTER_IP}:${PRINTER_PORT}; family-printer default via cups-pdf)"
