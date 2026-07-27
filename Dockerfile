FROM debian:13-slim

ENV DEBIAN_FRONTEND=noninteractive

# cups-pdf lives in the contrib component (and is named printer-driver-cups-pdf)
RUN sed -i '/^Components:/ s/$/ contrib/' /etc/apt/sources.list.d/debian.sources

RUN apt-get update && apt-get install -y --no-install-recommends \
    gettext-base \
    cups \
    cups-client \
    printer-driver-cups-pdf \
    cups-filters \
    printer-driver-hpcups \
    sssd \
    sssd-tools \
    sssd-ldap \
    libnss-sss \
    libpam-sss \
    supervisor \
    ca-certificates \
    python3 \
    && rm -rf /var/lib/apt/lists/*

# cups-pdf renders as the printing user into per-user subdirs of its spool, so the
# spool root must be user-writable (sticky, like cups-pdf's own default).
RUN mkdir -p /var/spool/cups-pdf && chmod 1777 /var/spool/cups-pdf

# rootfs mirrors the target filesystem; docker-entrypoint.sh renders *.template
# files (envsubst) and copies the rest into place at container start.
COPY rootfs /docker-entrypoint.d
COPY docker-entrypoint.sh /docker-entrypoint.sh
COPY healthcheck.sh /healthcheck.sh
RUN chmod +x /docker-entrypoint.sh /healthcheck.sh

EXPOSE 631 9101

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["/usr/bin/supervisord", "-n"]
