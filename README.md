# docker-cups-server

CUPS print proxy. Clients add **one** IPP printer (`family-printer`); the proxy
authenticates them against LDAP (SSSD), **archives** every job as PDF, then
**forwards** it to a physical printer over JetDirect (`socket://IP:9100`).

Everything site-specific (printer IP, PPD, LDAP directory, groups) is supplied at
runtime via environment variables — nothing is baked into the image.

Image: `ghcr.io/rclsilver-org/cups-server`

## How it works

```
client --IPP(631, Basic auth @printers)--> [family-printer / cups-pdf]
                                                   |
                                    PostProcessing (forward.sh)
                                    |                         |
                          /archive/<user>/YYYY/MM/DD/   lp -d real-printer
                          (read via Samba @administrators)      |
                                                        socket://IP:9100 (hpcups)
```

- **`family-printer`** — the only queue clients see. `cups-pdf` backend renders
  the job to PDF; the `PostProcessing` hook (`forward.sh`) stores it under
  `/archive/<user>/<year>/<month>/<day>/<HH-MM-SS>_<title>.pdf` and forwards the
  job to `real-printer`. Print operations require an authenticated member of the
  `printers` group (CUPS policy `authprint`).
- **`real-printer`** — the physical printer via a runtime-provided PPD
  (`PRINTER_PPD`) over `socket://${PRINTER_IP}:9100`. Printable **only** by the
  local system (the hook), never exposed to clients (CUPS policy `internalonly`).
- **`/archive`** — a hostPath volume (ZFS dataset on osmium); exposed read-only
  over Samba to the `administrators` group.

## Runtime configuration (env)

| Variable | Default | Purpose |
|---|---|---|
| `LDAP_URL` | – (required) | SSSD LDAP URI (e.g. `ldap://ldap.example.net:389`) |
| `LDAP_SEARCH_BASE` | – (required) | e.g. `dc=example,dc=net` |
| `LDAP_USER_SEARCH_BASE` | – (required) | e.g. `ou=users,dc=example,dc=net` |
| `LDAP_GROUP_SEARCH_BASE` | – (required) | e.g. `ou=groups,dc=example,dc=net` |
| `PRINTER_IP` | – (required) | physical printer IP |
| `PRINTER_PORT` | `9100` | physical printer JetDirect port |
| `PRINTER_PPD` | – (required) | path to the printer PPD (e.g. mounted from a ConfigMap) |
| `ARCHIVE_DIR` | `/archive` | archive root (mount the hostPath here) |
| `ARCHIVE_GROUP` | – (optional) | group given read access to the archive (else rely on the volume setgid) |

The CUPS print group is fixed to `printers` in the policy; adjust `cupsd.conf`
if you use a different group name.

## Layout

`rootfs/` mirrors the container filesystem; `docker-entrypoint.sh` renders
`*.template` files (envsubst) and copies the rest into place, then runs
`supervisord` (sssd + cupsd + a one-shot `cups-setup`).

Deployed via Terraform (`home-deployment-current`, namespace `mediacenter`), on
osmium (hostPath), behind a MetalLB LoadBalancer with external-dns.
