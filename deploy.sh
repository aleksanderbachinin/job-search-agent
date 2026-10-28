#!/usr/bin/env bash
# Выкладка агента на сервер из коммита, с проверкой после перезапуска.
#
#   ./deploy.sh          выложить HEAD (рабочая папка должна быть закоммичена)
#   ./deploy.sh prod     откатиться на прошлую удачную выкладку
#   ./deploy.sh <sha>    выложить любой коммит
#
# Шаги: тесты на копии коммита → rsync → uv sync и перезапуск → /healthz отдаёт
# этот коммит → в журнале нет ошибок. После успеха тег prod ставится на коммит,
# main и prod пушатся на GitHub (origin).

set -euo pipefail

HOST=root@193.42.112.144
APP=/opt/jobagent/app
HEALTHZ=https://193-42-112-144.sslip.io/healthz
# Сколько секунд после ответа /healthz смотреть журнал: бот и воркер стартуют следом.
SETTLE_SEC=20

cd "$(dirname "$0")"

fail() {
  echo "✗ $1" >&2
  if git rev-parse -q --verify prod >/dev/null; then
    echo "  Откат на прошлую выкладку: ./deploy.sh prod ($(git rev-parse --short=12 prod))" >&2
  fi
  exit 1
}

REF=${1:-HEAD}
if [ "$REF" = HEAD ] && [ -n "$(git status --porcelain)" ]; then
  git status --short >&2
  echo "✗ Есть незакоммиченные изменения: выкладывается только коммит." >&2
  exit 1
fi
SHA=$(git rev-parse --short=12 "$REF^{commit}")

BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD"' EXIT
git archive "$SHA" | tar -x -C "$BUILD"
echo "$SHA" > "$BUILD/agent/VERSION"

echo "1/5 Тесты на коммите $SHA"
(cd "$BUILD/agent" && uv run --frozen -q pytest -q -p no:cacheprovider) || { echo "✗ Тесты не прошли, на сервер ничего не ушло." >&2; exit 1; }

echo "2/5 Копирование на сервер"
# .env, .venv и база на сервере не трогаются: исключённые пути --delete не удаляет.
rsync -az --delete \
  --exclude .venv --exclude data --exclude __pycache__ --exclude .DS_Store --exclude agent/.env \
  "$BUILD"/ "$HOST:$APP/"

echo "3/5 Зависимости и перезапуск"
SINCE=$(ssh "$HOST" "date '+%Y-%m-%d %H:%M:%S'")
ssh "$HOST" "chown -R jobagent:jobagent $APP && cd $APP/agent \
  && sudo -u jobagent env HOME=/opt/jobagent UV_PYTHON_PREFERENCE=only-system uv sync --frozen --no-dev -q \
  && systemctl restart jobagent" || fail "uv sync или перезапуск не прошли."

echo "4/5 /healthz отдаёт $SHA"
for _ in $(seq 30); do
  if curl -fsS -m 5 "$HEALTHZ" 2>/dev/null | grep -q "\"version\":\"$SHA\""; then
    HEALTHY=1
    break
  fi
  sleep 2
done
[ -n "${HEALTHY:-}" ] || fail "За 60 с /healthz не отдал версию $SHA. Журнал: ssh $HOST journalctl -u jobagent -n 50"

echo "5/5 Журнал за $SETTLE_SEC с после старта"
sleep "$SETTLE_SEC"
LOG=$(ssh "$HOST" "systemctl is-active jobagent >/dev/null && journalctl -u jobagent --since '$SINCE' --no-pager -o cat") \
  || fail "Сервис не в состоянии active."
# Формат сервиса «… ERROR имя: …», у uvicorn «ERROR:    …». Сбои связи с Telegram
# («Failed to fetch updates») aiogram повторяет сам, это не ошибка выкладки.
ERRORS=$(grep -E "Traceback| ERROR |^ERROR:| CRITICAL |^CRITICAL:" <<<"$LOG" \
  | grep -v "aiogram.dispatcher: Failed to fetch updates" || true)
if [ -n "$ERRORS" ]; then
  echo "$ERRORS" | head -20 >&2
  fail "В журнале после перезапуска есть ошибки."
fi
grep -q "Сервис запущен" <<<"$LOG" || fail "В журнале нет строки «Сервис запущен»."

git tag -f prod "$SHA" >/dev/null
echo "✓ Выложен $SHA, тег prod обновлён."

# На GitHub: тег prod всегда, main только при выкладке HEAD (при откате main не трогаем).
# Сервер уже обновлён, поэтому сбой пуша — предупреждение, а не ошибка выкладки.
if [ "$REF" = HEAD ] && [ "$(git branch --show-current)" = main ]; then
  git push -q origin main || echo "⚠ main не запушен на GitHub: git push origin main" >&2
fi
git push -q -f origin prod || echo "⚠ Тег prod не запушен на GitHub: git push -f origin prod" >&2
