# Shared Roundcube webmail

When both the web and mail features are enabled, mrmpanel runs one Roundcube
container for the entire server. It is not copied into customer site folders.

Every managed domain and deployed site hostname is routed to:

```text
https://example.com/webmail/
```

Users sign in with their full mailbox address (for example,
`sales@example.com`) and the mailbox password created in the panel.

## How routing works

The panel writes `compose/traefik/dynamic/webmail.yml` whenever a domain or site
is added or removed. These `/webmail` routes have higher priority than customer
website routes and all point to `mrmpanel-roundcube`. HTTP is redirected to
HTTPS and `/webmail` is normalized to `/webmail/`.

Roundcube connects to docker-mailserver over the private `mrmpanel` Docker
network using IMAPS on port 993 and authenticated SMTP submission on port 587.
A single dedicated MariaDB container stores Roundcube sessions, contacts, and
preferences at:

```text
/var/lib/mrmpanel/roundcube/mysql
```

Mailbox messages remain in docker-mailserver's mail storage; Roundcube does not
duplicate them.
