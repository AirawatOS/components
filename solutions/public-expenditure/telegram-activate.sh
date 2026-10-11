#!/usr/bin/env bash
# ADR-0111 L4 — register an agency's Telegram webhook. YOU run this with YOUR bot token (from @BotFather); the
# token never leaves your shell. The bot TOKEN itself is NOT set here — you set it in the platform UI
# (Settings → Components → Telegram Concierge → TELEGRAM_BOT_TOKEN), where it is stored in Vault and the broker
# injects it into the adapter's egress. This script only points Telegram at the agency's webhook.
#   usage:  ./telegram-activate.sh <BOT_TOKEN> [tenant] [base_url] [webhook_secret]
set -euo pipefail
TOKEN="${1:?pass the BotFather token}"; TENANT="${2:-janaagraha}"; BASE="${3:-https://airos.airawat.org}"
SECRET="${4:-}"
URL="$BASE/t/$TENANT/telegram/webhook"
echo "Registering webhook: $URL"
curl -s "https://api.telegram.org/bot$TOKEN/setWebhook" \
  --data-urlencode "url=$URL" ${SECRET:+--data-urlencode "secret_token=$SECRET"} | python3 -m json.tool
echo
echo "Governed setup (one time, as the $TENANT tenant admin):"
echo "  1. Settings → Components → install 'Telegram Concierge' (ds:airawatdeveloper:adapter/telegram-concierge)"
echo "  2. Settings → Components → Telegram Concierge → set secret TELEGRAM_BOT_TOKEN = <your BotFather token>"
echo "The token lives in Vault; the confined adapter never sees it, the host never holds it."
echo "Then message your bot — it answers from $TENANT's governed capabilities, with sources."
