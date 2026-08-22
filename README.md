# mrmpanel

Docker hosting control panel for fresh Linux servers — sites, domains, mail,
databases, DNS, SSL, and webmail from one admin UI.

One host Docker engine, Traefik reverse proxy, one container per site hostname,
optional shared mail/SQL/DNS. No Docker-in-Docker.

> **Warning — not for production.** mrmpanel is early / experimental software.
> Do **not** use it for production workloads. If you install or run it, you do
> so **at your own risk**. There is no warranty; expect bugs, breaking changes,
> and incomplete features.

**Current release:** 0.1.30  
**Docs / install guides (human-readable):** https://hostingandstuff.online  
**Source:** https://github.com/cRaZyEyEs1987/mrmpanel  
**Safety rollback (pre-0.1.29):** branch [`safety/pre-0.1.29`](https://github.com/cRaZyEyEs1987/mrmpanel/tree/safety/pre-0.1.29) / tag [`v0.1.28`](https://github.com/cRaZyEyEs1987/mrmpanel/releases/tag/v0.1.28)

Compare panels, VPS picks, and mail caveats on the public site:

- [Install on Ubuntu 24.04](https://hostingandstuff.online/install/ubuntu-24-04/)
- [CyberPanel vs mrmpanel](https://hostingandstuff.online/compare/cyberpanel-vs-mrmpanel/)
- [Hestia vs mrmpanel](https://hostingandstuff.online/compare/hestia-vs-mrmpanel/)
- [Cheap VPS for a WordPress panel](https://hostingandstuff.online/compare/cheap-vps-wordpress-panel/)
- [Changelog / notes](https://hostingandstuff.online/notes/)

## Requirements

- Fresh AlmaLinux / Rocky / RHEL 9–10 or Ubuntu 24.04
- Root access (`sudo` is fine — the installing account becomes the file operator)
- No conflicting web, mail, database, or Docker services already installed

## Install

On a **new** server:

```bash
sudo bash -c "$(curl -fsSL https://mrmpanel.hostingandstuff.online/install.sh)" -- --all
```

That bootstrap pulls `mrmpanel-latest.tar.gz` and runs the full installer
(hostname + admin password prompts). On Alma/Rocky/RHEL 10, if Docker needs a
kernel reboot, install resumes automatically after boot.

```bash
export MRMPANEL_MIRROR=https://mrmpanel.hostingandstuff.online
curl -fsSL "$MRMPANEL_MIRROR/install.sh" | sudo bash -s -- --web --mail --mariadb
```

### Feature switches

| Flag | Meaning |
|------|---------|
| `--all` | web + mail + mariadb + postgres + dns |
| `--web` | Traefik + site hosting (default on) |
| `--mail` | docker-mailserver (+ Roundcube webmail when web is also on) |
| `--mariadb` | shared MariaDB |
| `--postgres` | shared PostgreSQL |
| `--dns` | PowerDNS authoritative DNS (default on) |
| `--no-web` / `--no-mail` / `--no-dns` / … | disable a feature |
| `--hostname FQDN` | set server hostname |
| `--non-interactive` | no prompts (needs `--hostname` and `MRMPANEL_ADMIN_PASSWORD`) |

Features are stored in `/var/lib/mrmpanel/features.json`.

## Upgrades

Install **latest** from the mirror (default). To pin an exact release for a
reproducible box, set `MRMPANEL_VERSION` (e.g. `0.1.30`) before running
`install.sh`.

- **Before any `--force` upgrade:** `sudo mrmpanel-backup`
- **Rollback on the same server:** `sudo mrmpanel-restore /var/backups/mrmpanel/….tar.gz`
- Details: [Backup & restore](docs/backup-restore.md)

## What you get

- **Admin panel** on `:8080`, with HTTPS on the server hostname via Traefik
- **Server overview** — PTR/RDNS and NS health, site + mail Start/Stop/Kill with
  usage snapshots, and a warning when domains lack MX/SPF/DKIM/DMARC
- **Hosting plans** — disk / domains / sites / mailboxes quotas (Infinite =
  unlimited); admin-only create/edit/assign
- **Hosting users** with jailed SSH/SFTP and `/home/<user>/…` layouts
- **Domains** owned by accounts (including separately owned subdomains)
- **Sites** — WordPress (auto-install), Laravel (Composer create-project),
  PHP, Node, Python stacks
- **Databases** — MariaDB / PostgreSQL, site-linked or standalone
- **Mail** — mailboxes under `/home/<user>/<email>/maildir`
- **Webmail** — one shared Roundcube at `https://<domain>/webmail/` (passwordless **Open in webmail** from the Mail menu)
- **Email security (users)** — per-domain SPF/DKIM/DMARC status and Enable missing
- **DNS** — PowerDNS zones, MX/SPF/DKIM/DMARC helpers, Domains page record table,
  Settings DNS debug for nameserver acceptance (any TLD; ZACR notes for `.za`)
- **SSL** — Let's Encrypt via Traefik; dashboard warnings + activate button
- **Operator ACL** — the sudo account that installed the panel can manage all
  `/home` files over SFTP without changing customer ownership

## After install

| Item | Location |
|------|----------|
| Panel | `http://<server-ip>:8080` (user `admin`) |
| Panel HTTPS | `https://<hostname>/` (when web + public DNS are ready) |
| Hosting users | same login URL → `/u/` |
| Data | `/var/lib/mrmpanel/` |
| App | `/opt/mrmpanel/` |
| Client homes | `/home/<user>/{domains,config,mail,plugins,logs}` |
| Webmail | `https://<managed-domain>/webmail/` |

Firewall ports opened for selected features: `8080`; web `80/443`; mail
`25/465/587/993`; MariaDB `3306`; Postgres `5432`; DNS `53/tcp+udp`.

With DNS enabled, hostname `server.example.com` → nameservers
`ns1.example.com` / `ns2.example.com` (same IP by default). Set glue at the
registrar.

## Docs

- [Accounts, domains, sites, databases, and mail](docs/accounts-and-domains.md)
- [Plans and admin dashboard](docs/plans-and-dashboard.md)
- [First site](docs/first-site.md)
- [DNS & PTR](docs/dns.md)
- [SSL](docs/ssl.md)
- [Webmail](docs/webmail.md)
- [Backup & restore](docs/backup-restore.md)

## Publishing updates (build / mirror host)

```bash
bash scripts/publish-release.sh
```

Publishes to `/var/www/mrmpanel-dist` for `mrmpanel.hostingandstuff.online`.

On an installed server, re-run the install script with `--force` (and the same
features) to upgrade.

## Development

```bash
bash scripts/dev-panel.sh
```

## License

MIT
