#!/usr/bin/env bash
# setup-server.sh — bootstrap an Ubuntu 22.04/24.04 ARM64 (Oracle A1.Flex) instance
# for the incident-response-agent stack.
#
# Run as the default `ubuntu` user:
#   curl -fsSL https://raw.githubusercontent.com/dide1/incident-response-agent/main/deploy/setup-server.sh | bash
# or clone first and run ./deploy/setup-server.sh

set -euo pipefail

REPO_URL="https://github.com/dide1/incident-response-agent.git"
APP_DIR="$HOME/incident-response-agent"

echo "── 1/5  Installing Docker (official apt repo, works on arm64) ──"
if ! command -v docker >/dev/null 2>&1; then
  sudo apt-get update -q
  sudo apt-get install -y -q ca-certificates curl gnupg
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
  sudo apt-get update -q
  sudo apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
  sudo usermod -aG docker "$USER"
  echo "Docker installed. NOTE: log out and back in for the docker group to apply,"
  echo "or prefix docker commands with sudo for this session."
else
  echo "Docker already installed: $(docker --version)"
fi

echo "── 2/5  Opening host firewall for the webhook port ──"
# Oracle Ubuntu images ship iptables rules that block most inbound traffic
# even when the VCN security list allows it. Allow 443 + 9000.
sudo iptables -C INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null \
  || sudo iptables -I INPUT 6 -p tcp --dport 443 -j ACCEPT
sudo iptables -C INPUT -p tcp --dport 9000 -j ACCEPT 2>/dev/null \
  || sudo iptables -I INPUT 6 -p tcp --dport 9000 -j ACCEPT
sudo apt-get install -y -q iptables-persistent 2>/dev/null || true
sudo netfilter-persistent save 2>/dev/null || true

echo "── 3/5  Cloning repo ──"
if [ ! -d "$APP_DIR" ]; then
  git clone "$REPO_URL" "$APP_DIR"
else
  echo "Repo already present at $APP_DIR"
fi

echo "── 4/5  Creating .env from template (fill in real secrets next) ──"
cd "$APP_DIR"
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "*** Edit $APP_DIR/.env now and set ANTHROPIC_API_KEY and SLACK_WEBHOOK_URL ***"
else
  echo ".env already exists — leaving it alone"
fi

echo "── 5/5  Done. Next steps ──"
cat <<'EOF'
  1. nano ~/incident-response-agent/.env      # paste real keys
  2. cd ~/incident-response-agent
  3. docker compose up -d --build             # first build ~5-10 min on A1
  4. docker compose ps                        # all 7 containers healthy?
  5. docker stats --no-stream                 # check memory headroom
  6. curl localhost:9000/health               # agent-backend responds?
  7. python3 -c "import urllib.request; print(urllib.request.urlopen('http://localhost:9000/health').read())"
  8. From your laptop: curl http://<public-ip>:9000/health
EOF
