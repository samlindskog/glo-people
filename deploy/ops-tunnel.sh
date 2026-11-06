#!/bin/bash
# The ops tunnel: a direct WireGuard link between the laptop and glousfr1 (no hub involved), over
# which the ops MCP and health (nginx :8800 -> the API's /ops/) are reachable and nothing else.
#   glousfr1: wg1, 10.89.0.1/24, udp/51821, peer = the laptop (10.89.0.2/32)
#   laptop:   WireGuard.app tunnel "glousfr1-ops", 10.89.0.2/24, endpoint <public ip>:51821,
#             AllowedIPs 10.89.0.0/24 (only the tunnel subnet is routed through it)
#
# Run on glousfr1 as root, once, with the laptop's public key:
#   sudo bash /opt/glo-people/deploy/ops-tunnel.sh <laptop-public-key>
# It installs wireguard, creates the server key if there is none, writes /etc/wireguard/wg1.conf,
# enables wg-quick@wg1, reloads nftables (deploy/nftables.conf already admits udp/51821 and the
# ops port from the laptop) and prints the server's public key for the laptop's config. Idempotent:
# run it again with a new laptop key to replace the peer.
set -euo pipefail
LAPTOP_PUBKEY="${1:-}"
[[ "$LAPTOP_PUBKEY" =~ ^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$ ]] || {
  echo "usage: $0 <laptop-public-key>   (wg genkey | tee priv | wg pubkey, on the laptop)" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 2; }

command -v wg >/dev/null || { apt-get -qq update && apt-get -y -qq install wireguard >/dev/null; }
install -d -m 700 /etc/wireguard
if [ ! -f /etc/wireguard/wg1.key ]; then
  umask 077
  wg genkey > /etc/wireguard/wg1.key
fi
SERVER_PRIV=$(cat /etc/wireguard/wg1.key)
SERVER_PUB=$(wg pubkey <<<"$SERVER_PRIV")

umask 077
cat > /etc/wireguard/wg1.conf <<CONF
# glo-people ops tunnel (deploy/ops-tunnel.sh). The laptop is the only peer.
[Interface]
Address = 10.89.0.1/24
ListenPort = 51821
PrivateKey = $SERVER_PRIV

[Peer]
# the laptop
PublicKey = $LAPTOP_PUBKEY
AllowedIPs = 10.89.0.2/32
CONF

systemctl enable wg-quick@wg1 >/dev/null
systemctl restart wg-quick@wg1
nft -c -f /etc/nftables.conf && systemctl reload nftables

echo "wg1 is up: $(ip -4 -brief addr show wg1 | awk '{print $3}')"
echo
echo "Server public key (paste into the laptop's tunnel config as the peer's PublicKey):"
echo "  $SERVER_PUB"
echo
echo "Laptop config (WireGuard.app > Add empty tunnel, name glousfr1-ops; fill in your PrivateKey):"
cat <<CONF
  [Interface]
  PrivateKey = <the laptop's private key>
  Address = 10.89.0.2/24

  [Peer]
  PublicKey = $SERVER_PUB
  Endpoint = $(curl -4 -s --max-time 5 https://ifconfig.me || echo "<glousfr1 public ip>"):51821
  AllowedIPs = 10.89.0.0/24
  PersistentKeepalive = 25
CONF
echo
echo "Then on the laptop: curl http://10.89.0.1:8800/ops/health"
