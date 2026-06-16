# startup.d — host-side startup hooks

Drop-in directory (conf.d-style) for **local, machine-specific** setup that must run
after `docker compose up`. `make up` (and `make startup`) execute every **executable**
`startup.d/*.sh` in name order, on the **host** — so hooks can do things the containers
can't, e.g. `docker network connect`, host route injection, `docker exec` into dind.

## Rules

- Scripts run **host-side**, in name order (`00-…`, `50-…`); prefix to control ordering.
- A script must be **executable** (`chmod +x`) or it's skipped.
- A non-zero exit aborts `make startup`.
- **Everything here is git-ignored except this README and `.gitignore`.** The mechanism
  is committed; your specifics stay local. Nothing machine-specific touches the tree.

## Why host-side (not inside dind)

dind's own `docker.sock` is its *nested* daemon — the host's networks aren't visible
there, so a script inside dind cannot attach dind to a host network. Host-side hooks can.

## Example: VPN routing (`50-vpn.sh`, local)

Gives the dind sandbox a leg on the VPN gateway's Docker network and mirrors the host's
VPN routes into dind, so the per-conversation toolboxes (via their squid proxy) reach
VPN-routed ranges. Idempotent; supports `up` (default) / `down` / `status`. Pair it with
a split-horizon resolver via `SANDBOX_PROXY_DNS` in the untracked `backend/.env`.

Re-run anytime with `make startup`, or directly: `./startup.d/50-vpn.sh status`.
