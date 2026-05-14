# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup
uv sync                                    # install all dependencies
uv sync --extra dev                        # include test dependencies
cp .env.example .env                       # configure API keys

# Run the agent (must be in project root so config/portfolio.yaml resolves correctly)
finance-agent run                          # daily analysis + Feishu push
finance-agent run --skip-notify            # dry run, print only
finance-agent run --no-backfill            # skip yesterday's win-rate backfill
finance-agent weekly-report --skip-notify  # weekly allocation report
finance-agent weekly-report --force        # re-run even if this week is cached
finance-agent daily-followup --skip-notify # Tue–Fri lightweight follow-up
finance-agent news-scan                    # scan holdings news, push if high-impact
finance-agent earnings-check --skip-notify # check upcoming earnings in 7 days
finance-agent generate-theses --force      # regenerate holding theses for all tickers
finance-agent generate-theses --ticker NVDA
finance-agent monthly-review --skip-notify

# Feedback loop
finance-agent log-action NVDA BUY --shares 1 --price 190
finance-agent show-actions --days 30
finance-agent feedback-stats

# Tests
pytest                                     # all tests
pytest tests/test_agents/test_debate.py   # single file
pytest -k "test_bull"                      # single test
```

The `finance-agent` CLI is installed via `pyproject.toml` entry point pointing to `finance_agent.main:app`.

## Architecture

### Daily pipeline (LangGraph 7-node DAG)

`graph/workflow.py` builds a linear `StateGraph`:

```
fetch_data → thesis → fundamentals → debate → decision → format → track
```

State flows through `AgentState` (Pydantic model in `graph/state.py`), which carries a list of `StockAnalysis` objects — one per holding. Each node returns an updated copy via `state.model_copy(update={...})`.

| Node | What it does |
|------|-------------|
| `fetch_data` | Parallel OHLCV + news + earnings fetch via `DataRouter`; computes `unrealized_pnl_pct`; also fetches macro context (VIX/yields/indices) |
| `thesis` | Loads per-ticker holding rationale from the `theses` SQLite table into each `StockAnalysis` |
| `fundamentals` | Claude (via CLI subprocess) writes a plain-language fundamental view; ETFs (QQQM, VOO) are hardcoded to skip |
| `debate` | DeepSeek Bull then Bear analysis sequentially (serial to avoid rate limits); DCA tickers skip to fixed strings |
| `decision` | Single batch Claude call for Portfolio Manager verdict across all stocks at once |
| `format` | Builds both `report_text` (console) and `report_card` (Feishu interactive card JSON) |
| `track` | Saves today's recommendations to SQLite; backfills 7-day returns for old records |

### Model routing

- **DeepSeek V3** — Bull/Bear debate (`agents/bull_agent.py`, `agents/bear_agent.py`). Called via OpenAI-compatible API (`DEEPSEEK_API_KEY`).
- **Claude** — Fundamental analysis + Portfolio Manager batch decision + weekly advisor. Called via `claude -p` subprocess (`agents/claude_client.py`). Requires `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`. Falls back to DeepSeek if Claude CLI is unavailable.
- All prompts live in `agents/prompts.py`.

### Data layer

`data/router.py` dispatches by `market` field:
- `us` → `YFinanceProvider` (yfinance)
- `hk` → `AkShareProvider` (东方财富 / AkShare)
- `cn` → `AkShareProvider`

Port stock tickers use `00700` format; `YFinanceProvider` adds `.HK` suffix when needed. Currency normalisation: HKD ÷ 7.8, CNY ÷ 7.2 → USD for concentration calculations.

### Persistence (SQLite at `data/agent.db`)

Three tables, created lazily at runtime:
- `recommendations` — daily ticker recommendations with 7-day return backfill and outcome scoring (`tracker.py`)
- `theses` — AI-generated holding rationale per ticker, with `pillars` (JSON) and `stop_conditions` (`thesis_generator.py`)
- `user_actions` — manual BUY/SELL/SKIP records linked to recommendations, with 7-day price backfill (`tracker.py`)

The `daily_signals` table in `storage/schema.sql` / `storage/db.py` is a separate write path used by `main.py` to snapshot raw signal scores.

DB path defaults to `data/agent.db` relative to CWD, overridable via `AGENT_DB_PATH` env var. Always run `finance-agent` commands from the project root.

### Weekly pipeline

`weekly/allocation_advisor.py` runs three sequential Claude calls:
1. Concentration diagnosis → hedge directions
2. Hedge instrument selection per direction
3. Opportunity screening (RSI < 48 pre-filter + Claude fundamental double-check)

Result cached to `data/weekly_latest.json` keyed by ISO week; `--force` bypasses cache.

### Feishu notifications

`notifications/feishu.py` — HMAC-SHA256 signed webhook. Two functions: `send_feishu_card` (interactive card JSON) and `send_feishu_message` (plain text fallback). Requires `FEISHU_WEBHOOK_URL`; `FEISHU_WEBHOOK_SECRET` is optional.

`notifications/glossary.py` scans report text for financial jargon and appends plain-language definitions to the card (max 5 terms).

### Automation

Six GitHub Actions workflows trigger the CLI commands on schedule (Beijing time). Secrets needed: `DEEPSEEK_API_KEY`, `FEISHU_WEBHOOK_URL`, and one of `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY`.

## Key configuration files

- `config/portfolio.yaml` — holdings list with `ticker`, `market` (us/hk/cn), `shares`, `cost_basis`, `sector`, `peers`, `is_dca` flag
- `config/settings.yaml` — model parameters (temperature, max_tokens) and signal thresholds (RSI overbought/oversold, MA periods, `min_confidence`). **`send_hour: 9` is a dead config value — it is never read by any code; scheduling must be done externally via crontab or GitHub Actions.**
- `.env` — `DEEPSEEK_API_KEY` (required), `FEISHU_WEBHOOK_URL`, `FEISHU_WEBHOOK_SECRET`, `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY`
