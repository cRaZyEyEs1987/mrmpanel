# SSL certificates

mrmpanel uses Traefik with Let's Encrypt. Certificates are free, issued
automatically, and renewed automatically. There is nothing to buy or upload.

## The panel itself

After install the panel is reachable three ways:

- `http://SERVER_IP:8080` — always works, no certificate needed
- `http://hostname:8080` — same thing, by name
- `https://hostname` — HTTPS with a real certificate (port 80 redirects here)

The HTTPS address is set up by the installer for the server hostname (for
example `server.example.com`). The admin dashboard shows a green banner once the
certificate is trusted, so you can tell at a glance whether it worked.

The dashboard also checks the current browser session. If it was opened through
an `http://` address, a red warning shows the exact unsafe link and directs the
administrator to the secure hostname. HTTP remains available as a recovery path
for now; a separate option to disable it can be added later.

Requirements for the certificate to be issued:

1. The hostname has a public A record pointing at this server.
2. Ports 80 and 443 are open (the installer opens them in firewalld/ufw; also
   check any cloud firewall or security group).

If issuance has not happened yet the dashboard shows a yellow banner and
`:8080` keeps working, so you are never locked out. Press **Activate SSL** in
that banner to open a live progress window. It checks the hostname and DNS,
starts Traefik, requests the certificate, and reports either the successful
issuer and expiry date or an actionable error.

## Customer sites

When you deploy a site, the panel checks whether the domain already resolves to
this server:

- **It does** — the site is created with the Let's Encrypt resolver, and Traefik
  fetches a certificate within about a minute. `https://domain` just works.
- **It does not** — the site still answers on HTTPS, but with Traefik's built-in
  self-signed certificate, so browsers show a warning. This is expected while
  DNS is still pointing elsewhere.

Once the domain starts pointing here, open **Sites** and press **Enable HTTPS**
on that row. The panel re-checks DNS and, if the domain now resolves to this
server, recreates the container so Traefik requests the real certificate. If DNS
is not ready yet you get a plain message saying so — pressing it again later is
safe.

## Checking a certificate from the shell

```bash
echo | openssl s_client -connect example.com:443 -servername example.com 2>/dev/null \
  | openssl x509 -noout -issuer -dates
```

`issuer=... O=Let's Encrypt` means the real certificate is in place.
`issuer=CN=TRAEFIK DEFAULT CERT` means it is still the self-signed fallback.

## Troubleshooting

- **Still the Traefik default certificate.** DNS for the domain does not resolve
  to this server yet, or port 80 is blocked so Let's Encrypt cannot validate.
  Verify with `dig +short A example.com` and retry Enable HTTPS.
- **Certificate for the panel hostname never appears.** Check the hostname is a
  real FQDN with a public A record: `dig +short A "$(hostname -f)"`.
- **Traefik logs.** `docker logs mrmpanel-traefik-1 | grep -i acme` shows every
  certificate request and any Let's Encrypt error.
- **Rate limits.** Let's Encrypt allows a limited number of failed attempts per
  hour. The panel refuses to request certificates for domains that do not point
  here yet, which is what keeps you clear of those limits.
