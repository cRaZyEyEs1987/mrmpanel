# mrmpanel

CWP-like Docker hosting panel for fresh Linux servers.

One host Docker engine, one reverse proxy (Traefik), one container per site/hostname, optional shared mail and SQL. No Docker-in-Docker.

## Requirements

- Fresh AlmaLinux/Rocky/RHEL 9–10 or Ubuntu 24.04
- Root access
- No conflicting web/mail/database/Docker services already installed

## Install (single file — downloads the rest)

On a **new** server:

```bash
sudo bash -c "$(curl -fsSL https://mrmpanel.hostingandstuff.online/install.sh)" -- --all
```

That bootstrap script pulls `mrmpanel-latest.tar.gz` from the same host, then runs the full installer (prompts for hostname and admin password; skips the feature menu). On Alma/Rocky/RHEL 10, if a kernel reboot is required for Docker, the installer reboots and **finishes automatically** — no second command.

Override mirror if needed:

```bash
export MRMPANEL_MIRROR=https://mrmpanel.hostingandstuff.online
curl -fsSL "$MRMPANEL_MIRROR/install.sh" | sudo bash -s -- --web --mariadb
```

### Feature switches

| Flag | Meaning |
|------|---------|
| `--all` | web + mail + mariadb + postgres + dns |
| `--web` | Traefik + site hosting (default on) |
| `--mail` | docker-mailserver |
| `--mariadb` | shared MariaDB |
| `--postgres` | shared PostgreSQL |
| `--dns` | PowerDNS authoritative DNS (default on); ns1/ns2 from hostname |
| `--no-web` / `--no-mail` / `--no-dns` / … | disable a feature |
| `--hostname FQDN` | set server hostname (skips interactive PTR flow) |
| `--non-interactive` | no prompts (requires `--hostname` and admin password env) |

Feature selection is written to `/var/lib/mrmpanel/features.json` and drives the panel UI.

## Publishing updates (on the build server)

```bash
bash scripts/publish-release.sh
```

Publishes to `/var/www/mrmpanel-dist` for subdomain `mrmpanel.hostingandstuff.online` only (does not change other sites).

## After install

- Panel: `http://<server-ip>:8080` (user `admin`, password shown at end of install)
- Hosting users sign in at the same URL and land on `/u/`
- Data: `/var/lib/mrmpanel/`
- App: `/opt/mrmpanel/`
- Client homes: `/home/<user>/{domains,config,mail,plugins,logs}`
- Mailboxes (when mail enabled): `/home/<user>/<email@domain>/maildir` (Maildir); docker-mailserver reaches them via symlink from `/var/lib/mrmpanel/mail/data/`
- Traefik (if web): ports 80/443 → client site containers
- Installer opens host firewall ports for selected features (firewalld/ufw): panel `8080`; web `80/443`; mail `25/465/587/993`; MariaDB `3306`; Postgres `5432`; DNS `53/tcp+udp`
- With DNS enabled: hostname `server.example.com` → nameservers `ns1.example.com` / `ns2.example.com` (same IP by default; editable in Settings). Set glue at your registrar.

## Development

```bash
bash scripts/dev-panel.sh
```

## License

MIT
