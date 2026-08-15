# mrmpanel

Docker hosting control panel for fresh Linux servers — sites, domains, mail,
databases, DNS, SSL, and webmail from one admin UI.

One host Docker engine, Traefik reverse proxy, one container per site hostname,
optional shared mail/SQL/DNS. No Docker-in-Docker.

**Current release:** 0.1.21  
**Source:** https://github.com/cRaZyEyEs1987/mrmpanel

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

## What you get

- **Admin panel** on `:8080`, with HTTPS on the server hostname via Traefik
- **Hosting users** with jailed SSH/SFTP and `/home/<user>/…` layouts
- **Domains** owned by accounts (including separately owned subdomains)
- **Sites** — WordPress (auto-install), Laravel (Composer create-project),
  PHP, Node, Python stacks
- **Databases** — MariaDB / PostgreSQL, site-linked or standalone
- **Mail** — mailboxes under `/home/<user>/<email>/maildir`
- **Webmail** — one shared Roundcube at `https://<domain>/webmail/`
- **DNS** — PowerDNS zones, MX/SPF/DKIM/DMARC helpers, Domains page record table
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
- [First site](docs/first-site.md)
- [DNS & PTR](docs/dns.md)
- [SSL](docs/ssl.md)
- [Webmail](docs/webmail.md)

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
