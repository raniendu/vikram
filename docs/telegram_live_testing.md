# Telegram Live Testing

1. Create a bot with BotFather and set:

```env
VIKRAM_TELEGRAM_BOT_TOKEN=...
VIKRAM_TELEGRAM_WEBHOOK_SECRET=...
VIKRAM_TELEGRAM_ALLOWED_CHAT_IDS=123456789
VIKRAM_TELEGRAM_BOT_USERNAME=VikramBot
```

2. Run the API:

```bash
uv run vikram-api
```

3. Expose it through an HTTPS tunnel, then register the webhook:

```bash
uv run python -m vikram.local_webhook https://example.ngrok-free.app
```

4. Send a text message from an allowed chat.

In groups, Vikram responds only when mentioned by username or when replying to
one of its messages. Group `/reset` and `/agent` commands require Telegram admin
status.
