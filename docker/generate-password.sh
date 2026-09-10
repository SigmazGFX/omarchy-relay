#!/usr/bin/env bash
# Adds (or updates) one username/password in docker/passwd, creating the file
# with correct hashing via the mosquitto image itself (no local mosquitto
# install required). Run once per user you want on the broker.
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "usage: $0 <username> [password]" >&2
  echo "  (omit password to be prompted for it)" >&2
  exit 1
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USERNAME="$1"
FLAG=-b
ARGS=("$USERNAME")
if [ $# -ge 2 ]; then
  ARGS+=("$2")
else
  FLAG=-B  # prompt for password interactively inside the container
fi

CREATE_FLAG=""
[ -f "$DIR/passwd" ] || CREATE_FLAG="-c"

docker run --rm -it \
  -v "$DIR:/mosquitto/config" \
  eclipse-mosquitto:2 \
  mosquitto_passwd $CREATE_FLAG "$FLAG" /mosquitto/config/passwd "${ARGS[@]}"

echo "Updated $DIR/passwd"
