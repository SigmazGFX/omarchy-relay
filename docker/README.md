# Self-hosted broker

This runs a private [Mosquitto](https://mosquitto.org/) MQTT broker with
username/password auth, so you're not relaying chat/files through someone
else's public broker. omarchy-relay's own payload encryption (network
passphrase) protects message *content* even from the broker operator, but
the broker still sees who's talking to whom and how often — self-hosting
avoids handing that metadata to a third party too.

## Quick start

```sh
cd docker
./generate-password.sh alice          # prompts for a password; repeat per user
docker compose up -d
```

The broker listens on `1883` (plain TCP, no TLS — see below). Point every
device's config at it:

```toml
[broker]
host = "<the box running docker compose>"
port = 1883
tls = false
username = "alice"
password = "..."
```

## Getting devices without direct connectivity to reach this broker

Pick one:

1. **Private overlay network (recommended, simplest).** Put the broker box
   and every client on a [Tailscale](https://tailscale.com/) or WireGuard
   network. Traffic is already encrypted point-to-point, so plain MQTT
   (`tls = false`) over the overlay is fine, and you never open a port on
   the public internet. Set `broker.host` to the box's Tailscale IP/name.

2. **Public broker + TLS.** If the broker box needs a public IP, put it
   behind a TLS-terminating reverse proxy that supports raw TCP streams
   (e.g. [Caddy's `layer4`](https://github.com/mholt/caddy-l4) module, or
   `stunnel`), or generate your own certs and add a `listener 8883` +
   `certfile`/`keyfile`/`cafile` block to `mosquitto.conf`. Then set
   `broker.tls = true` and `broker.port` to the TLS listener's port.
   Credentials + payloads both travel in the clear on port 1883 otherwise —
   don't expose that port to the internet.

3. **Managed MQTT provider.** HiveMQ Cloud, EMQX Cloud, etc. have free
   tiers with TLS already set up — skip this docker-compose entirely and
   point the config at the provider's host/port/credentials with
   `broker.tls = true`.

## Adding/removing users

```sh
./generate-password.sh <username> <password>   # add or change a password
docker exec -it $(docker compose ps -q mosquitto) mosquitto_passwd -D /mosquitto/config/passwd <username>  # remove
docker compose restart mosquitto
```
