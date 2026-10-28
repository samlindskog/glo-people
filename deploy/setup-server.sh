#!/bin/bash
# One-time (idempotent) server preparation for glousfr1. Run as root:
#   sudo bash setup-server.sh "<deploy public key>"
set -euo pipefail
DEPLOY_PUBKEY=${1:?deploy public key required}
SRC=$(cd "$(dirname "$0")" && pwd)
APP=/opt/glo-people
export DEBIAN_FRONTEND=noninteractive

# 1 GB swap: the Nanode has 1 GB RAM for nginx + api + postgres + mongo
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo 'vm.swappiness = 10' > /etc/sysctl.d/91-swap.conf && sysctl -q --system
fi

# Firewall first: the new ruleset only manages table inet filter, so Docker's tables survive reloads
install -d -m 755 /etc/nftables.d
touch /etc/nftables.d/cloudflare.nft
install -m 755 "$SRC/nftables.conf" /etc/nftables.conf
nft -c -f /etc/nftables.conf
systemctl enable nftables >/dev/null
systemctl reload nftables

# Docker CE
if ! command -v docker >/dev/null; then
  apt-get -qq update && apt-get -y -qq install ca-certificates curl gnupg >/dev/null
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $VERSION_CODENAME stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get -qq update
  apt-get -y -qq install docker-ce docker-ce-cli containerd.io docker-compose-plugin >/dev/null
fi
cat > /etc/docker/daemon.json <<JSON
{ "log-driver": "local", "log-opts": {"max-size": "10m", "max-file": "3"}, "live-restore": true, "no-new-privileges": true }
JSON
systemctl restart docker

# deploy user: SSH key restricted to the deploy script
id deploy >/dev/null 2>&1 || adduser --disabled-password --comment "" deploy >/dev/null
usermod -aG docker deploy
install -m 755 "$SRC/deploy.sh" /usr/local/bin/glo-deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
echo "restrict,command=\"/usr/local/bin/glo-deploy\" $DEPLOY_PUBKEY" > /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys && chmod 600 /home/deploy/.ssh/authorized_keys
# sshd: `sam` administers over port 22, CI deploys over port 2222, which nftables opens only to the
# GitHub Actions runner ranges. Splitting the two is the point: the forced-command deploy key is then
# unusable from anywhere but a runner. The Match blocks go at the END of the main sshd_config, not in a
# drop-in: drop-ins are included from the top, and a Match there would swallow the `Subsystem` line
# that follows it, which sshd rejects inside a Match block.
HARDENING=/etc/ssh/sshd_config.d/00-hardening.conf
sed -i 's/^AllowUsers .*/AllowUsers sam deploy/' $HARDENING
# The drop-in is provisioned out of band, so every key is set idempotently and appended when absent;
# a bare `sed -i s/^Key .*/` would silently no-op on a host whose drop-in never had the key.
set_sshd_key() {  # set_sshd_key <keyword> <value>
  if grep -qiE "^[[:space:]]*$1[[:space:]]" $HARDENING; then
    sed -i -E "s|^[[:space:]]*$1[[:space:]]+.*|$1 $2|I" $HARDENING
  else
    printf '%s %s\n' "$1" "$2" >> $HARDENING
  fi
  grep -qxE "$1 $2" $HARDENING || { echo "sshd: failed to set '$1 $2' in $HARDENING" >&2; exit 1; }
}
grep -qE '^[[:space:]]*Port[[:space:]]+22$'   $HARDENING || echo 'Port 22'   >> $HARDENING
grep -qE '^[[:space:]]*Port[[:space:]]+2222$' $HARDENING || echo 'Port 2222' >> $HARDENING
# uscentral1's grace time; the rest of this drop-in is already stricter than the hub
# (MaxAuthTries 3, publickey-only, no agent forwarding, no X11).
set_sshd_key LoginGraceTime 20
if ! grep -q '^# BEGIN glo-people' /etc/ssh/sshd_config; then
  cat >> /etc/ssh/sshd_config <<'SSHD'

# BEGIN glo-people (managed by deploy/setup-server.sh; keep last, Match swallows what follows)
Match LocalPort 2222
  AllowUsers deploy
Match LocalPort 22
  AllowUsers sam
# END glo-people
SSHD
fi
sshd -t
# Prove the split before reloading: deploy must be refused on 22 and sam refused on 2222.
sshd -T -C user=sam,addr=192.0.2.1,lport=22      | grep -qx 'allowusers sam'    || { echo "sshd: port 22 is not sam-only" >&2; exit 1; }
sshd -T -C user=deploy,addr=192.0.2.1,lport=2222 | grep -qx 'allowusers deploy' || { echo "sshd: port 2222 is not deploy-only" >&2; exit 1; }
systemctl reload ssh

# App directory
install -d -m 755 -o deploy -g deploy $APP $APP/nginx
install -d -m 750 -o root -g deploy $APP/certs
install -m 644 -o deploy -g deploy "$SRC/../compose.yaml" "$SRC/../compose.prod.yaml" $APP/
if [ ! -f $APP/.env ]; then
  umask 077
  cat > $APP/.env <<ENV
POSTGRES_PASSWORD=$(openssl rand -hex 24)
EMBEDDING_PROVIDER=openai
OPENAI_API_KEY=
IMAGE_TAG=latest
ENV
  chown deploy:deploy $APP/.env
fi

# Authenticated Origin Pulls: certs/aop-ca.pem is the CA that signed the client cert uploaded to
# Cloudflare. Start in `optional` mode; switch to `on` once AOP is enabled for the zone.
if [ ! -f $APP/nginx/aop.conf ]; then
  printf '# Authenticated Origin Pulls: optional until enabled in Cloudflare, then on\nssl_verify_client optional;\n' \
    > $APP/nginx/aop.conf
fi

# Cloudflare ranges → nftables sets + nginx real-IP, refreshed weekly
install -m 755 "$SRC/cf-ips.sh" /usr/local/sbin/glo-cf-ips
cat > /etc/systemd/system/glo-cf-ips.service <<UNIT
[Unit]
Description=Refresh the Cloudflare and GitHub Actions IP ranges for glo-people
After=network-online.target nftables.service
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/glo-cf-ips
UNIT
cat > /etc/systemd/system/glo-cf-ips.timer <<UNIT
[Unit]
Description=Daily Cloudflare + GitHub Actions IP refresh
[Timer]
OnCalendar=daily
RandomizedDelaySec=3h
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now glo-cf-ips.timer >/dev/null
/usr/local/sbin/glo-cf-ips
echo "setup complete"
