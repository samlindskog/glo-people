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
sed -i 's/^AllowUsers .*/AllowUsers sam deploy/' /etc/ssh/sshd_config.d/00-hardening.conf
sshd -t && systemctl reload ssh

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
Description=Refresh Cloudflare IP ranges for glo-people
After=network-online.target nftables.service
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/glo-cf-ips
UNIT
cat > /etc/systemd/system/glo-cf-ips.timer <<UNIT
[Unit]
Description=Weekly Cloudflare IP refresh
[Timer]
OnCalendar=weekly
RandomizedDelaySec=6h
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now glo-cf-ips.timer >/dev/null
/usr/local/sbin/glo-cf-ips
echo "setup complete"
