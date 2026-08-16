# Shared Roundcube webmail

When both the web and mail features are enabled, mrmpanel runs one Roundcube
container for the entire server. It is not copied into customer site folders.

Every managed domain and deployed site hostname is routed to:

```text
https://example.com/webmail/
```

## Sign-in options

1. **Passwordless from the panel** — on the Mail page, click **Open in webmail**
   next to a mailbox. The panel issues a one-time token (about 60 seconds) and
   Roundcube signs in via a Dovecot master account. No mailbox password is
   required for that session.
2. **Manual login** — open `/webmail/` and sign in with the full mailbox address
   and the mailbox password created in the panel.

## How routing works

The panel writes `compose/traefik/dynamic/webmail.yml` whenever a domain or site
is added or removed. These `/webmail` routes have higher priority than customer
website routes and all point to `mrmpanel-roundcube`. HTTP is redirected to
HTTPS and `/webmail` is normalized to `/webmail/`.

Roundcube connects to docker-mailserver over the private `mrmpanel` Docker
network using IMAPS on port 993 and authenticated submission on port 587. In an
SSO session both connections authenticate as the Dovecot master user, so the
mailbox password is never needed. A single dedicated MariaDB container stores
Roundcube sessions, contacts, and preferences at:

```text
/var/lib/mrmpanel/roundcube/mysql
```

Mailbox messages remain in docker-mailserver's mail storage; Roundcube does not
duplicate them.

SSO tokens are stored briefly under `/var/lib/mrmpanel/webmail-sso/`. The master
password used only by Roundcube lives in
`/var/lib/mrmpanel/secrets/webmail_master_password`.

Roundcube's session cipher key is pinned from
`/var/lib/mrmpanel/secrets/roundcube_des_key`. Without it the container would
generate a new key on every recreate, and open webmail tabs would fail with
`Connection to IMAP server failed. Server Error: Empty password`.
