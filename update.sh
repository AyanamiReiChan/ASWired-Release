#!/usr/bin/env bash
set -euo pipefail
umask 077
exec 9>/run/lock/aswired-update.lock
flock -n 9 || { echo 'An update is already running.' >&2; exit 2; }
version=${1:-}
[[ $EUID == 0 && $version =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]] || { echo 'Usage: sudo bash update.sh vX.Y.Z [--database-backup /absolute/pg-backup]' >&2; exit 2; }
[[ -L /opt/aswired/current && -f /etc/aswired/server.env ]] || { echo 'Managed installation not found. Read the migration guide.' >&2; exit 2; }
case "$(uname -m)" in x86_64) arch=amd64;; aarch64|arm64) arch=arm64;; *) echo 'Unsupported architecture.' >&2; exit 2;; esac
db_backup=''
if [[ -e /var/lib/aswired/database-active.enc ]] || grep -Eq '^ASWIRED_DATABASE_DRIVER=postgres' /etc/aswired/server.env; then
  [[ ${2:-} == --database-backup && ${3:-} == /* && -s ${3:-} ]] || { echo 'PostgreSQL requires a verified fresh database backup. Pass --database-backup /absolute/path and read the PostgreSQL upgrade notes.' >&2; exit 2; }
  db_backup=$3
fi
target="/opt/aswired/releases/$version"
[[ ! -e $target ]] || { echo 'Target version directory already exists; do not overwrite installed releases.' >&2; exit 2; }
temporary=$(mktemp -d)
trap 'rm -rf -- "$temporary"' EXIT
asset="aswired_${version}_linux_${arch}.tar.gz"
url="https://github.com/AyanamiReiChan/ASWired-Release/releases/download/$version"
curl --fail --location --connect-timeout 20 --max-time 60 --proto '=https' --proto-redir '=https' "$url/SHA256SUMS" -o "$temporary/SHA256SUMS"
curl --fail --location --connect-timeout 20 --max-time 600 --proto '=https' --proto-redir '=https' "$url/$asset" -o "$temporary/$asset"
expected=$(awk -v name="$asset" '$2==name {print $1}' "$temporary/SHA256SUMS")
[[ $expected =~ ^[0-9a-fA-F]{64}$ ]] || { echo 'Missing or duplicate checksum.' >&2; exit 1; }
printf '%s  %s\n' "$expected" "$temporary/$asset" | sha256sum --check --status
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python3 "$base/deploy/extract-release.py" "$temporary/$asset" "$temporary/extracted"
package="$temporary/extracted/aswired"
[[ $(cat "$package/VERSION") == "$version" ]] || { echo 'Package version mismatch.' >&2; exit 1; }
"$package/bin/aswired-server" version | grep -F "$version" >/dev/null
"$package/runtime/node" --version >/dev/null
"$package/bin/komari" --help >/dev/null
previous=$(readlink -f /opt/aswired/current)
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup="/var/backups/aswired/$stamp"
install -d -m 0700 "$backup"
printf '%s\n' "$previous" > "$backup/previous-release"
echo "Stopping services for a consistent backup: $backup"
# Before switching programs it is safe to restart the old services on failure.
# After switching, retain migrated data and the backup for explicit recovery.
switched=0
recover_before_switch() {
  result=$?
  if (( result != 0 && switched == 0 )); then systemctl start aswired-server komari aswired-web || true; fi
  rm -rf -- "$temporary"
}
trap recover_before_switch EXIT
systemctl stop aswired-web komari aswired-server
if [[ -e /var/lib/aswired/database-pending.enc ]] || { [[ -e /var/lib/aswired/database-active.enc ]] && [[ -z $db_backup ]]; }; then
  echo 'Database state changed while downloading; upgrade cancelled before switching.' >&2; exit 1
fi
if ! tar --exclude=aswired/update-request.json --exclude=aswired/site-certificate-request.json --exclude=aswired/upgrade-backup-request.json --exclude=aswired/agent-releases --exclude=aswired/backups --exclude=aswired/logs -czf "$backup/data.tar.gz" -C /var/lib aswired komari; then
  systemctl start aswired-server komari aswired-web
  echo 'Backup failed; update cancelled.' >&2; exit 1
fi
if ! tar -czf "$backup/config.tar.gz" -C /etc aswired; then
  systemctl start aswired-server komari aswired-web
  echo 'Configuration backup failed; update cancelled.' >&2; exit 1
fi
if [[ -n $db_backup ]]; then cp -- "$db_backup" "$backup/postgres-backup"; fi
cp -a "$package" "$target"
chmod -R a+rX "$target"
chown -R root:root "$target"
ln -s "$target" /opt/aswired/.next
mv -Tf /opt/aswired/.next /opt/aswired/current
switched=1
# Refresh only installation assets; existing node Agents are upgraded manually.
for cpu in amd64 arm64; do
  install -o root -g aswired -m 0750 "$target/agent-releases/linux-$cpu/aswired-agent" "/var/lib/aswired/agent-releases/linux-$cpu/.aswired-agent-new"
  mv -f "/var/lib/aswired/agent-releases/linux-$cpu/.aswired-agent-new" "/var/lib/aswired/agent-releases/linux-$cpu/aswired-agent"
done
install -m 0644 "$target"/deploy/systemd/*.service /etc/systemd/system/
install -m 0644 "$target"/deploy/systemd/*.path /etc/systemd/system/
install -d -o root -g aswired -m 0750 /var/lib/aswired-updater
install -d -o root -g root -m 0755 /var/lib/aswired-certificates
systemctl daemon-reload
systemctl enable --now aswired-update.path aswired-certificates.path aswired-upgrade-backups.path
if ! systemctl start aswired-server komari aswired-web || ! python3 "$target/deploy/verify-health.py" --version "$version"; then
  echo "Startup verification failed. Backup: $backup. Follow docs/UPGRADE.md before reverting databases." >&2
  exit 1
fi
echo "Updated to $version. Consistent backup: $backup"
echo 'HTTPS proxy, accounts, keys and environment settings were preserved. Check both websites and one Agent before deleting backups.'
