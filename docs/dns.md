# DNS & PTR

## Authoritative DNS (ns1 / ns2)

When DNS is enabled at install (default), mrmpanel runs PowerDNS on this server.

From hostname `server.example.com` the installer derives:

| Role | Name | Default IP |
|------|------|------------|
| NS1 | `ns1.example.com` | public IP |
| NS2 | `ns2.example.com` | same public IP (change later in Settings) |

### Registrar glue

At the registrar for **example.com**, create host/glue records:

```
ns1.example.com  →  YOUR.SERVER.IP
ns2.example.com  →  YOUR.SERVER.IP
```

Then set the domain’s nameservers to `ns1.example.com` and `ns2.example.com`.

#### `.co.za` (ZACR) specifics

ZACR validates that nameservers are **reachable and authoritative** before accepting an NS update. If the check fails, the change is declined (or the domain stays on `serverHold` / unpublished).

Practical order for `example.co.za` with in-bailiwick NS (`ns1.example.co.za` / `ns2.example.co.za`):

1. Install mrmpanel so PowerDNS has the zone (AA answers for SOA/NS/A).
2. **Before** changing NS at the registrar: on the *current* DNS (old host), create A records:
   - `ns1.example.co.za` → this server’s IP  
   - `ns2.example.co.za` → this server’s IP  
   Until those exist, public lookups for `ns1`/`ns2` return **NXDOMAIN**, and many registrar CheckDNS UIs reject the change even though PowerDNS on the new IP is already authoritative.
3. At the registrar, use **Glue records** / **Register nameserver** (child hosts) for `ns1` and `ns2` with this server’s IP — do not only type the hostnames without IPs.
4. Then set the domain nameservers to those two hosts.
5. Wait for the registry check (often minutes; retries can run for hours). Contact updates can lock other changes for ~5 days.

Same IP for ns1 and ns2 is OK on one server. A second IP for ns2 only works if that IP also answers authoritatively for the zone.

Until the base domain’s NS+glue are published in the `.co.za` zone, `ns1.example.co.za` will not resolve publicly — so other `.co.za` domains cannot use those nameservers yet.

## Debug nameserver acceptance

On the server (after install/upgrade that includes this script):

```bash
sudo bash /opt/mrmpanel/scripts/dns-debug.sh
sudo bash /opt/mrmpanel/scripts/dns-debug.sh yourdomain.com
sudo bash /opt/mrmpanel/scripts/dns-debug.sh yourdomain.co.za
```

Or in the panel: **Settings → Run DNS debug**.

The report separates **server-side** failures (zone/PowerDNS) from **parent
delegation** (what the registry parent zone — `com`, `co.za`, etc. — publishes)
and a **public resolver** view (may lag).

### Customer domains

Every site deployed on this server gets a PowerDNS zone whose **NS** records point at the panel ns1/ns2 (not at the customer domain). At each customer’s registrar, set nameservers to the same ns1/ns2 hostnames.

The **Domains** page (admin and hosting user) shows a **Nameservers** column: a public resolver check against Settings ns1/ns2 (`pointed` / `partial` / `other NS` / `not pointed`). Hover the badge for detail. It can lag briefly after a registrar change; use Settings → DNS debug for the full checklist.

Default zone contents on deploy:

- `A` for apex and `www` → server public IP
- `NS` → panel ns1/ns2
- If mail is enabled: `MX` + SPF `TXT`

Change ns1/ns2 IPs (or hostnames) in the panel **Settings** page.

## PTR (reverse DNS)

Your server’s public IP should reverse-resolve to the same hostname you set during install.

Example:

```
203.0.113.10  →  server1.example.com
```

If PTR is missing or wrong, the installer warns you. Mail providers often reject mail from IPs without matching PTR.

## Recommended records (when not using panel DNS)

Replace `example.com`, `server1.example.com`, and `203.0.113.10`.

```
; Web
example.com.        IN A     203.0.113.10
www.example.com.    IN A     203.0.113.10

; Mail (when mail module installed)
example.com.        IN MX 10 server1.example.com.
example.com.        IN TXT   "v=spf1 mx a:server1.example.com ip4:203.0.113.10 ~all"
_dmarc.example.com. IN TXT   "v=DMARC1; p=none; rua=mailto:dmarc@example.com"
```

DKIM TXT is generated in the panel after you enable DKIM for the domain.

## SOA serial numbers

Zones use the `YYYYMMDDnn` convention. The date part trails UTC by 12 hours so
it is never ahead of the calendar date in any timezone — checkers such as
MxToolbox compare the serial date against their own clock and report
"SOA Serial Number Format is Invalid" when it looks like tomorrow.

PowerDNS would otherwise overwrite these values, so the panel clears the
`SOA-EDIT` / `SOA-EDIT-API` zone metadata and bumps the serial itself on every
record change.

## BIMI (brand logo in inboxes)

BIMI is optional and only takes effect once DMARC is at `p=quarantine` or
`p=reject` with `pct=100`. Publish an SVG Tiny PS logo over HTTPS and add:

```
default._bimi.example.com. IN TXT "v=BIMI1; l=https://example.com/bimi/logo.svg;"
```

The logo must be a square SVG with `baseProfile="tiny-ps"`, a `<title>`, and no
scripts, external references, or raster images. Gmail and Apple Mail also
require a Verified Mark Certificate (paid, needs a registered trademark), added
as `a=https://example.com/bimi/vmc.pem`; without it the record is valid but most
inboxes will not render the logo.

## Let’s Encrypt

Point the domain’s A record at this server (or use panel DNS + registrar NS) before deploying a site so Traefik can issue certificates.
