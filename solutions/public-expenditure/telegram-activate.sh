#!/usr/bin/env bash
# ADR-0111 L4 — activate the Telegram messaging adapter for an agency tenant. YOU run this with YOUR bot token
# (from @BotFather); the token never leaves your shell. It (1) tells Telegram to deliver updates to the agency's
# webhook, and (2) reminds you to set the same token in the app-host env.
#   usage:  ./telegram-activate.sh <BOT_TOKEN> [tenant] [base_url] [webhook_secret]
set -euo pipefail
TOKEN="${1:?pass the BotFather token}"; TENANT="${2:-janaagraha}"; BASE="${3:-https://airos.airawat.org}"
SECRET="${4:-}"
URL="$BASE/t/$TENANT/telegram/webhook"
echo "Registering webhook: $URL"
curl -s "https://api.telegram.org/bot$TOKEN/setWebhook" \
  --data-urlencode "url=$URL" ${SECRET:+--data-urlencode "secret_token=$SECRET"} | python3 -m json.tool
echo
echo "Now set the token in app-host env and recreate it (on the EC2 host):"
echo "  cd ~/airawatos && sed -i 's#TELEGRAM_BOT_TOKEN: \"\"#TELEGRAM_BOT_TOKEN: \"'"$TOKEN"'\"#' docker-compose.yml"
echo "  docker compose up -d --force-recreate app-host"
echo "Then message your bot on Telegram — it answers from $TENANT's governed capabilities, with sources."
