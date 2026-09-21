#!/usr/bin/env bash
set -euo pipefail
umask 077
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
panel=${1:-}; probe=${2:-}
[[ $EUID == 0 ]] || { echo 'Run as root: sudo bash install.sh panel.example.com probe.example.com' >&2; exit 2; }
valid_domain() { [[ $1 =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$ && $1 == *.* && $1 != *..* ]]; }
valid_domain "$panel" && valid_domain "$probe" && [[ $panel != "$probe" ]] || { echo 'Supply two distinct DNS hostnames (without https:// or paths).' >&2; exit 2; }
for program in systemctl curl openssl python3 nginx install tar; do command -v "$program" >/dev/null || { echo "Missing $program; read README.md prerequisites." >&2; exit 2; }; done
[[ -d /run/systemd/system ]] || { echo 'A running systemd host is required.' >&2; exit 2; }
version=$(cat "$base/VERSION")
[[ $version =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]] || { echo 'Invalid package version.' >&2; exit 2; }
for path in /opt/aswired/current /etc/aswired /var/lib/aswired /var/lib/komari /etc/systemd/system/aswired-server.service /etc/systemd/system/aswired-web.service /etc/systemd/system/komari.service /etc/nginx/sites-available/aswired.conf; do
  [[ ! -e $path && ! -L $path ]] || { echo "Existing installation found at $path. Use update.sh or the migration guide; nothing was overwritten." >&2; exit 2; }
done
"$base/runtime/node" --version >/dev/null
"$base/bin/aswired-server" version | grep -F "$version" >/dev/null
"$base/bin/komari" --help >/dev/null
id aswired >/dev/null 2>&1 || useradd --system --home-dir /var/lib/aswired --shell /usr/sbin/nologin aswired
id aswired-web >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin aswired-web
id komari >/dev/null 2>&1 || useradd --system --home-dir /var/lib/komari --shell /usr/sbin/nologin komari
install -d -m 0755 /opt/aswired/releases
[[ ! -e /opt/aswired/releases/$version ]] || { echo 'Version directory already exists.' >&2; exit 2; }
cp -a "$base" "/opt/aswired/releases/$version"
chmod -R a+rX "/opt/aswired/releases/$version"
chown -R root:root "/opt/aswired/releases/$version"
ln -s "releases/$version" /opt/aswired/current
install -d -o aswired -g aswired -m 0700 /var/lib/aswired
install -d -o komari -g komari -m 0700 /var/lib/komari
install -d -m 0700 /etc/aswired
bridge=$(openssl rand -hex 32)
cat > /etc/aswired/server.env <<EOF
ASWIRED_LISTEN=127.0.0.1:12889
ASWIRED_DATA_DIR=/var/lib/aswired
ASWIRED_PUBLIC_URL=https://$panel
ASWIRED_ALLOWED_ORIGINS=https://$panel
ASWIRED_DATABASE_DRIVER=sqlite
ASWIRED_KOMARI_PUBLIC_URL=https://$probe
ASWIRED_KOMARI_BRIDGE_SECRET=$bridge
EOF
cat > /etc/aswired/komari.env <<EOF
KOMARI_LISTEN=127.0.0.1:25774
KOMARI_DB_FILE=/var/lib/komari/data/komari.db
ASWIRED_IDENTITY_URL=http://127.0.0.1:12889
ASWIRED_LOGIN_URL=https://$panel
KOMARI_PUBLIC_URL=https://$probe
ASWIRED_BRIDGE_SECRET=$bridge
EOF
cat > /etc/aswired/web.env <<EOF
NODE_ENV=production
HOST=127.0.0.1
PORT=3000
ORIGIN=https://$panel
EOF
unset bridge
chmod 0600 /etc/aswired/*.env
install -d -o aswired -g aswired -m 0700 /var/lib/aswired/agent-releases
cp -a "$base/agent-releases/." /var/lib/aswired/agent-releases/
chown -R root:aswired /var/lib/aswired/agent-releases
chmod -R u=rwX,g=rX,o= /var/lib/aswired/agent-releases
install -m 0644 "$base"/deploy/systemd/*.service /etc/systemd/system/
sed -e "s/PANEL_DOMAIN/$panel/g" -e "s/PROBE_DOMAIN/$probe/g" "$base/deploy/nginx.conf" > /etc/nginx/sites-available/aswired.conf
chmod 0644 /etc/nginx/sites-available/aswired.conf
ln -s /etc/nginx/sites-available/aswired.conf /etc/nginx/sites-enabled/aswired.conf
nginx -t
systemctl daemon-reload
systemctl enable --now aswired-server aswired-web komari
systemctl reload nginx
curl --fail --silent --show-error --retry 20 --retry-all-errors --retry-delay 1 http://127.0.0.1:12889/healthz >/dev/null
echo "Installed ASWired $version and integrated Komari. No administrator was created."
echo "Next: sudo certbot --nginx -d $panel -d $probe"
echo "Then open https://$panel and choose the administrator username and password."
echo 'Read the one-time setup token locally: sudo cat /var/lib/aswired/setup-token'
