# Plans and admin dashboard

## Admin server overview

The admin home page (`/`) is a **server overview**, not a feature catalogue. It shows:

- **PTR / RDNS** for the public IP vs the panel hostname
- **NS1 / NS2** A records vs the IPs stored in Settings
- **Panel HTTP** — disable/re-enable public `:8080` once the hostname SSL cert is trusted
- **Mail container** status with Start / Stop / Kill (errors from a failed start are shown briefly)
- **Infrastructure** — Traefik, MariaDB, PostgreSQL, PowerDNS Start / Stop / Kill when those features are on
- **Sites** with Docker status, CPU/memory snapshot, and Start / Stop / Kill
- A warning when managed domains are missing **MX / SPF / DKIM / DMARC**

SSL and unsafe-HTTP alerts remain at the top of the page. Panel forms are CSRF-protected.

## Hosting plans (quotas)

Plans live in `/var/lib/mrmpanel/plans.json`. Only administrators create, edit, or delete plans (Users page).

Default seeded plans:

| Plan | Disk | Domains | Sites | Mailboxes |
|------|------|---------|-------|-----------|
| Starter | 10 GB | 1 | 1 | 5 |
| Business | 50 GB | 5 | 5 | 25 |
| Unlimited | Infinite | Infinite | Infinite | Infinite |

- Leave a limit blank in the UI for **Infinite** (`null` in JSON).
- Assign a plan when creating a user, or change it later on the Users table.
- Soft enforcement: creating a **domain**, **site**, or **mailbox** fails when that quota is already met. Disk is checked when deploying a new site (`du` on `/home/<user>`).
- Existing users without `plan_id` are treated as **Unlimited** and backfilled on upgrade.

Hosting users see usage bars on their dashboard; they cannot edit plans.

## Per-domain email security (hosting users)

On `/u/`, each managed domain shows MX / SPF / DKIM / DMARC status. **Enable missing** publishes safe defaults when this server hosts the PowerDNS zone; otherwise the action reports the records to add at an external DNS provider.

Admins still have the global scan under **Settings → Global mail authentication**, plus the dashboard gap warning.
