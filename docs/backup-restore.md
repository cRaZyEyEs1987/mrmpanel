# Backup and restore

Same-server rollback for upgrades. Back up **before** any
`install.sh … --force` run.

> Experimental software — verify restores on a test box when you can.
> These commands replace live data.

## Backup

```bash
sudo mrmpanel-backup
```

Creates `/var/backups/mrmpanel/mrmpanel-YYYYMMDD-HHMMSS.tar.gz` (mode `600`).
Optional path:

```bash
sudo mrmpanel-backup /root/mrmpanel-pre-upgrade.tar.gz
```

### Included

- `/var/lib/mrmpanel` (secrets, features, site/domain/user metadata, SQL data
  dirs, mail config/state, Roundcube DB, PowerDNS DB, …)
- `/home/<hosting-user>` for each account under `/var/lib/mrmpanel/users/`
- `/etc/mrmpanel` (when present)
- Traefik ACME volume `mrmpanel_traefik_letsencrypt`

### Excluded

- `/opt/mrmpanel` application code (reinstall from a release/tag)
- Ephemeral webmail SSO tokens (`webmail-sso/*.json`)
- Mail log files (`mail/logs/**`)

During backup, MariaDB/Postgres/Roundcube DB/mail/DNS containers are briefly
stopped for a consistent filesystem copy; Traefik stays up when possible.

## Restore

mrmpanel must already be installed on the server (compatible version).

```bash
sudo mrmpanel-restore /var/backups/mrmpanel/mrmpanel-YYYYMMDD-HHMMSS.tar.gz
```

Non-interactive:

```bash
sudo mrmpanel-restore --yes /path/to/archive.tar.gz
```

Restore stops the panel and compose stack, replaces data/homes/certs, brings
services back up, and recreates site containers from saved metadata.

Then verify panel UI, sites/HTTPS, mail + webmail SSO, and DNS.

## Pin an exact release

Default install is **latest**. To reproduce a specific build:

```bash
export MRMPANEL_VERSION=0.1.30
curl -fsSL https://mrmpanel.hostingandstuff.online/install.sh | sudo bash -s -- --all
```

Always run `sudo mrmpanel-backup` before `--force` upgrades.