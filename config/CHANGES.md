# OpenTrader — Change Log

## Phase 2 Entry — LLM + Notification Updates

### LLM connector switched to OpenRouter
- Single API key, access to all major models
- Model routing per agent (predictor, review, eod, orchestrator)
- Automatic fallback to secondary model on failure
- Retry with exponential backoff
- File: python/llm/connector.py

### Email switched from SMTP to AgentMail
- Each agent gets its own inbox identity
- Thread tracking built in
- Webhook-ready for inbound mail handling
- File: python/notifier/agentmail.py

### Notification channels
- AgentMail (agent-native email)
- Telegram (real-time alerts + fills)
- Discord (three webhooks: alerts, trades, EOD)

### Message routing (system.toml)
- trade_fill     → telegram + discord_trades
- eod_report     → agentmail_eod + discord_eod + telegram
- review_output  → agentmail_review + discord_eod + telegram
- system_alert   → telegram + discord_alerts + agentmail_alerts
- circuit_break  → telegram + discord_alerts + agentmail_alerts
