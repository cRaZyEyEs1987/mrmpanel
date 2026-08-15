# Accounts, domains, sites, databases, and mail

The administrator sidebar has a **View account** selector. **All users** shows
the complete server. Selecting one hosting user limits Sites, Databases, Mail,
dashboard counts, and account-aware tools to that user. **Global settings**
always remains server-wide.

## Installer operator access

When the installer is launched with `sudo`, mrmpanel records the original sudo
account (for example, `mark`) as the server operator. POSIX ACLs grant that
account read, write, and directory traversal access throughout `/home`, so it
can manage every hosting user's files over SFTP/FileZilla.

File ownership and the original group/other access entries remain assigned to
each hosting user. `ls -l` adds a trailing `+`; its displayed group bits become
the ACL mask, so they may look broader even though the owning group entry has
not been broadened. Default ACLs propagate operator access to new users,
domains, and files, and the panel reapplies the ACL after application
deployments. A direct root install does not add an operator ACL because root
already has access.

## Domain ownership

A domain is assigned to a hosting account before sites or mailboxes are added.
This separates domain ownership from a particular web container.

- `example.com` can belong to account `alice`.
- Alice can deploy the main `example.com` site and subdomains such as
  `shop.example.com`.
- `frank.example.com` can instead be added as its own managed domain and
  assigned by the administrator to account `frank`, giving it separate panel,
  filesystem, site, database, and mailbox credentials. Hosting users cannot
  delegate part of another account's domain themselves.

The most-specific assignment wins. If `frank.example.com` is separately
assigned, the owner of `example.com` cannot also create a site using that exact
hostname. Likewise, an existing site hostname must be deleted before the same
hostname can be added as a separate managed domain.

A domain assignment cannot be deleted while sites covered by that assignment
still exist.

## Mail

Mail domains come only from the account's managed Domains list. Creating a web
site does not silently create an unrelated mail domain. The mailbox form uses a
mailbox name plus a domain selector, so an administrator cannot accidentally
create mail under the server hostname or another customer’s domain.

## Global mail authentication

**Global settings → Global mail authentication** scans public DNS for every
managed domain:

- SPF: exactly one `v=spf1` policy with an `all` or `redirect` mechanism
- DKIM: selector `mail` with a non-empty public-key policy
- DMARC: one `v=DMARC1` policy containing `p=`

Enabling a control preserves an existing valid record. If PowerDNS on this
server hosts the zone, mrmpanel publishes a safe missing default. For external
DNS it reports that the record must be added at the external provider. The
default DMARC policy is `p=quarantine; pct=100`, which satisfies common
deliverability and BIMI pre-checks. Global settings also offers strict
`p=reject` and monitoring-only `p=none`; the latter does not satisfy BIMI.
The same scan checks that each mail domain has at least one MX record and
publishes this server's mail hostname when the managed PowerDNS zone is missing
one.
