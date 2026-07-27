#!/usr/bin/env bash

set -e

# LDAP settings consumed by rootfs/etc/sssd/sssd.conf.template (auth + @printers/@administrators resolution)
export LDAP_URL=${LDAP_URL:-''}
export LDAP_SEARCH_BASE=${LDAP_SEARCH_BASE:-''}
export LDAP_USER_SEARCH_BASE=${LDAP_USER_SEARCH_BASE:-''}
export LDAP_GROUP_SEARCH_BASE=${LDAP_GROUP_SEARCH_BASE:-''}

# LDAP group whose members are allowed to print (used in cupsd.conf.template)
export PRINT_GROUP=${PRINT_GROUP:-printers}

# cupsd log verbosity (used in cupsd.conf.template). cupsd logs go to stderr so
# they surface in the container logs; bump to "debug" to trace auth/job flow.
export CUPS_LOG_LEVEL=${CUPS_LOG_LEVEL:-warn}

# Render templates / copy plain files from the rootfs mirror into place.
find /docker-entrypoint.d/ -type f | while read filename; do
  out_dir="/$(dirname "${filename#/docker-entrypoint.d/}")"
  out_file=$(basename "${filename}")

  mkdir -p "${out_dir}"

  if [[ "$filename" == *.template ]]; then
    out_file="${out_file%.template}"
    echo "Processing template ${filename} -> ${out_dir}/${out_file}"
    envsubst < "${filename}" > "${out_dir}/${out_file}"
  else
    echo "Copying ${filename} -> ${out_dir}/${out_file}"
    cp -p "${filename}" "${out_dir}/${out_file}"
  fi

  # sssd refuses to start if sssd.conf is not 0600
  if [[ "${out_file}" == "sssd.conf" ]]; then
    chmod 600 "${out_dir}/${out_file}"
  fi
done

# CUPS 1.6+ keeps AccessLog/ErrorLog/PageLog in cups-files.conf (NOT cupsd.conf).
# Send AccessLog/ErrorLog to stderr (so cupsd logs surface in the container logs)
# but keep PageLog as a file (the exporter parses it for per-user page accounting).
if [[ -f /etc/cups/cups-files.conf ]]; then
  sed -i -E 's#^[[:space:]]*(AccessLog|ErrorLog)[[:space:]]+.*#\1 stderr#' /etc/cups/cups-files.conf
  sed -i -E 's#^[[:space:]]*PageLog[[:space:]]+.*#PageLog /var/log/cups/page_log#' /etc/cups/cups-files.conf
  grep -qiE '^ErrorLog ' /etc/cups/cups-files.conf || echo 'ErrorLog stderr' >> /etc/cups/cups-files.conf
  grep -qiE '^AccessLog ' /etc/cups/cups-files.conf || echo 'AccessLog stderr' >> /etc/cups/cups-files.conf
  grep -qiE '^PageLog ' /etc/cups/cups-files.conf || echo 'PageLog /var/log/cups/page_log' >> /etc/cups/cups-files.conf
fi

exec "$@"
