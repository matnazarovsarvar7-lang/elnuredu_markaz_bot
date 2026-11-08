#!/usr/bin/env bash
# ELNUR_EDU bot — serverga o'rnatish. Ishga tushirish: bash install.sh
set -e
DIR=/opt/elnur-bot
echo "== ELNUR_EDU bot o'rnatilmoqda =="

FREE_GB=$(df -BG / | awk 'NR==2 {gsub("G","",$4); print $4}')
if [ "$FREE_GB" -lt 5 ]; then echo "Diskda joy kam: ${FREE_GB} GB. Kamida 5 GB kerak."; exit 1; fi

command -v docker >/dev/null || { apt-get update -y; apt-get install -y docker.io docker-compose-v2; }
command -v git >/dev/null || apt-get install -y git

if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only; else git clone https://github.com/matnazarovsarvar7-lang/elnuredu_markaz_bot.git "$DIR"; fi
cd "$DIR"

if [ ! -f .env ]; then
  echo
  read -rsp "BotFather bergan tokenni joylashtiring va Enter bosing (ekranda ko'rinmaydi): " TOKEN; echo
  read -rp "Rahbar telefon raqami (masalan 998906482494): " OWNER; echo
  DBPASS=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)
  printf "BOT_TOKEN=%s\nDB_PASSWORD=%s\nOWNER_PHONE=%s\n" "$TOKEN" "$DBPASS" "$OWNER" > .env
  chmod 600 .env
fi

docker compose up -d --build

# Har kuni 03:00 da baza nusxasi, 14 kunlik saqlanadi
mkdir -p /opt/elnur-backups
cat > /etc/cron.d/elnur-backup <<CRON
0 3 * * * root cd $DIR && docker compose exec -T db pg_dump -U elnur elnur | gzip > /opt/elnur-backups/elnur-\$(date +\%F).sql.gz && find /opt/elnur-backups -name '*.sql.gz' -mtime +14 -delete
CRON

echo
echo "== Tayyor! Bot ishga tushdi. Telegramda botingizga /start yozing. =="
docker compose ps
