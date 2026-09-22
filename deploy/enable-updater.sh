#!/usr/bin/env bash
set -euo pipefail
[[ $EUID == 0 ]] || { echo 'Run as root.' >&2; exit 2; }
base=/opt/aswired/current
[[ -L $base && -f $base/deploy/update-request.py ]] || { echo 'Install a release with managed updates first.' >&2; exit 2; }
[[ ! -L /var/lib/aswired-updater ]] || { echo 'Unsafe updater state directory.' >&2; exit 2; }
if [[ -e /var/lib/aswired-updater ]]; then
  [[ -d /var/lib/aswired-updater && $(stat -c %u /var/lib/aswired-updater) == 0 ]] || { echo 'Updater state must be a root-owned directory.' >&2; exit 2; }
fi
install -d -o root -g aswired -m 0750 /var/lib/aswired-updater
install -m 0644 "$base/deploy/systemd/aswired-update.service" "$base/deploy/systemd/aswired-update.path" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now aswired-update.path
echo 'ASWired web updates enabled.'
