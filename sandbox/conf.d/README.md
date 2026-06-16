# sandbox/conf.d — squid ACL drop-in fragments

`sandbox/squid.conf` ends its ACL section with `include /conf.d/*.conf`. The backend
bind-mounts this directory into every proxy at `/conf.d` (read-only, via
`SANDBOX_SQUID_CONFD`), so **local, site-specific** policy can extend the egress allowlist
without editing the checked-in `squid.conf`.

> **Why `/conf.d` and not `/etc/squid/conf.d`?** The stock `ubuntu/squid` image ships its
> own `/etc/squid/conf.d/debian.conf` containing `http_access allow localnet` — including
> that would short-circuit the deny rules and open egress entirely. A dedicated `/conf.d`
> keeps the vendor's file out of scope and fails **closed** (squid FATALs) if unmounted.

## Rules

- One `*.conf` per concern; files load in name order (`00-…`, `10-…`).
- ACLs with the same name **accumulate** — e.g. another `acl allowed_domains dstdomain …`
  line here widens `squid.conf`'s `allowed_domains`.
- The `include` is placed **before** `http_access`, so fragment ACLs are defined when the
  rules evaluate.
- **Fragments must contain ONLY `acl` definitions — never `http_access`.** Because the
  include precedes the access rules, an `http_access allow …` here would be evaluated
  first and could silently bypass the S3-exfil deny and the default-deny. These files are
  git-ignored (unreviewed), so keep them to widening `allowed_domains` only.
- **Everything here is git-ignored except `00-defaults.conf`, this README, and `.gitignore`.**
  The mechanism is committed; your fragments stay local.
- `00-defaults.conf` must stay — squid **FATALs** if the include glob matches zero files.

## Example (local, git-ignored): `10-vpn.conf`

```
# Corporate VPN split-horizon hosts — resolved via SANDBOX_PROXY_DNS, reached via the
# startup.d VPN routing. This only opens the squid allowlist; DNS + L3 are wired elsewhere.
acl allowed_domains dstdomain .private.example.com .private.example.dev
```

Changes apply to **newly created** proxies (per conversation); existing ones keep their
mounted snapshot until rebuilt.
