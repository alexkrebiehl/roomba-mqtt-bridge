#!/bin/sh
# One-off: put the Roomba bridge's three secrets in krebiehl-base-secrets.
#   roomba-blid, roomba-password  - the robot's local credentials, from a JSON
#                                   file with "blid" and "password" (as pulled
#                                   from HA's roomba config entry)
#   mosquitto-roomba-password     - newly generated broker password for the
#                                   `roomba` account
# Values go through 0600 temp files (az --file), never argv or the terminal.
#
# Usage: deploy/kv-set-secrets.sh path/to/roomba_creds.json
set -eu
CREDS=${1:?usage: $0 roomba_creds.json}
VAULT=krebiehl-base-secrets
D=$(mktemp -d)
trap 'rm -rf "$D"' EXIT
chmod 700 "$D"

python3 - "$CREDS" "$D" <<'EOF'
import json, os, secrets, sys
creds, d = json.load(open(sys.argv[1])), sys.argv[2]
# Alphanumeric: mosquitto's init container passes it to mosquitto_passwd -b.
alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
vals = {
    "roomba-blid": creds["blid"],
    "roomba-password": creds["password"],
    "mosquitto-roomba-password": "".join(secrets.choice(alphabet) for _ in range(40)),
}
for name, value in vals.items():
    fd = os.open(os.path.join(d, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, value.encode())
    os.close(fd)
EOF

for n in roomba-blid roomba-password mosquitto-roomba-password; do
  az keyvault secret set --vault-name "$VAULT" --name "$n" --file "$D/$n" \
    --query "{name:name,updated:attributes.updated}" -o tsv
done
