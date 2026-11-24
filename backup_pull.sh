#!/usr/bin/env bash
# Забирает свежий ночной бэкап агента с сервера на этот Mac.
# Запускается раз в день launchd (~/Library/LaunchAgents/com.jobagent.backup-pull.plist),
# можно и руками: ./backup_pull.sh
#
# Ключ ~/.ssh/id_ed25519_jobagent_backup без пароля, на сервере он ограничен командой
# /usr/local/bin/jobagent-backup-send: может только отдать последний архив.
# Архивы: agent/data/backups/, хранятся 30 последних.

set -euo pipefail

HOST=root@138.249.207.239
KEY="$HOME/.ssh/id_ed25519_jobagent_backup"
DEST="$(cd "$(dirname "$0")" && pwd)/agent/data/backups"
KEEP=30

mkdir -p "$DEST"
TS=$(date -u +%Y%m%d-%H%M)
TMP="$DEST/.pull-$TS.tar.gz"
CHECK=$(mktemp -d)
trap 'rm -rf "$TMP" "$CHECK"' EXIT

ssh -T -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=20 "$HOST" > "$TMP"
tar -xzf "$TMP" -C "$CHECK" ./jobagent.sqlite3
[ "$(sqlite3 "$CHECK/jobagent.sqlite3" 'pragma integrity_check')" = ok ] || { echo "✗ integrity_check не прошёл" >&2; exit 1; }
COUNT=$(sqlite3 "$CHECK/jobagent.sqlite3" 'select count(*) from vacancies')

mv "$TMP" "$DEST/jobagent-$TS.tar.gz"
ls -1t "$DEST"/jobagent-*.tar.gz | tail -n +$((KEEP + 1)) | xargs -r rm -f
echo "$(date '+%F %T') ✓ $DEST/jobagent-$TS.tar.gz, вакансий $COUNT"
