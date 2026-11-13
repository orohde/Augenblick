#!/bin/sh
# Started as root (e.g. from an Unraid template): make /data writable for PUID:PGID,
# then drop privileges. Started with --user: nothing to do.
# The photo folders are never chowned.
set -e
if [ "$(id -u)" = "0" ]; then
  PUID="${PUID:-99}"; PGID="${PGID:-100}"
  umask "${UMASK:-0002}"
  mkdir -p "${AUGENBLICK_DATA:-/data}"
  chown -R "$PUID:$PGID" "${AUGENBLICK_DATA:-/data}"
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$@"
fi
[ -n "$UMASK" ] && umask "$UMASK"
exec "$@"
