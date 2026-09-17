# Marketing Approval Bot

A Slack-native decision-support system for marketing approvals. It turns a structured campaign request into a financial evaluation, posts the result back to the request thread, records the decision, and follows the campaign through approval and post-campaign actuals.

The system was built to replace a slow, manual approval loop with an auditable workflow that can return a first-pass recommendation in minutes.

## What it does

- Accepts requests through a `/promo` Slack modal, Slack Workflow Builder messages, or an HTTP webhook.
- Parses retailer, market, channel, discount, spend, commission, dates, and campaign type.
- Pulls market economics, sales history, promo history, return rates, and lookup tables from Google Sheets.
- Uses live purchase and advertising data through an internal data API, with bounded fallbacks when a source is unavailable.
- Models unit economics and incremental contribution margin, then returns a grade, recommendation, assumptions, and risk flags.
- Supports retailer promotions, D2C campaigns, influencer campaigns, newsletter sponsorships, and non-financial marketing requests.
- Tracks pending, approved, rejected, and no-response requests in a marketing tracker.
- Reads approval or rejection replies from Slack threads and limits approvals to configured users.
- Reconciles missed approvals after restarts or transient integration failures.
- Sends calendar, campaign-start, and post-campaign actuals reminders through a Kubernetes CronJob.

## Architecture

```text
Slack command / Workflow Builder / webhook
                  |
                  v
        FastAPI + Slack Bolt adapter
                  |
          parser / Slack form parser
                  |
        data collection and normalization
       /          |           |          \
Google Sheets  Data API   Promo history  Ad metrics
       \          |           |          /
                  v
        evaluation and grading engine
                  |
       Slack response + Marketing Tracker
                  |
      approval listener / reconciliation job
                  |
       reminders and post-campaign actuals
```

### Main modules

| Module | Responsibility |
| --- | --- |
| `app/main.py` | FastAPI routes, Slack event handlers, request orchestration |
| `app/parser.py` / `app/slack_form.py` | Free-text and structured request parsing |
| `app/evaluator.py` | Retail and influencer economics, grading, and recommendations |
| `app/evaluator_d2c.py` | D2C campaign evaluation |
| `app/evaluator_newsletter_sponsorship.py` | Newsletter sponsorship economics |
| `app/sheets.py` / `app/lookups.py` | Google Sheets data access and cached lookup tables |
| `app/ad_metrics.py` / `app/d2c_history.py` | Live sales and advertising data access |
| `app/tracker.py` | Decision tracking, approval state, reminders, and actuals |
| `app/reconcile.py` | Recovery of approvals missed by the live event flow |
| `app/responder.py` | Slack Block Kit responses |
| `app/reminders_cron.py` | One-shot scheduled reminder and reconciliation job |

The application is intentionally stateless at the web tier. Google Sheets holds the durable workflow record, while Slack threads provide the human decision trail. The reconciliation job covers the failure mode where an event is received but the process restarts before the tracker is updated.

## Tech stack

- Python 3.12
- FastAPI and Uvicorn
- Slack Bolt
- Google Sheets via `gspread`
- Anthropic API as an optional parsing and commentary fallback
- Docker
- Kubernetes and Helm
- GitHub Actions with AWS ECR/EKS deployment
- `pytest` for unit and integration-style tests

## Local setup

### 1. Create a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Fill in `.env` with credentials and resource IDs for your environment. Never commit `.env` or a service-account key.

Required for application startup:

- `SLACK_BOT_TOKEN`
- `SLACK_SIGNING_SECRET`
- `GOOGLE_SERVICE_ACCOUNT_JSON`
- `PROMO_SHEET_ID`
- `PL_SHEET_ID`
- `SELLOUT_SHEET_ID`

Required for the complete production flow:

- `PROMO_CHANNEL_ID`
- `APPROVER_USER_ID`
- `APPROVER_USER_IDS`
- `APPROVER_APPROVALS_CHANNEL_ID`
- `SLACK_TEAM_ID`
- `DATA_API_URL`
- `DATA_API_TOKEN`

Optional:

- `ANTHROPIC_API_KEY`
- `METABASE_URL`
- `METABASE_API_KEY`
- `DEBUG_TOKEN` - enables authenticated debug and manual-trigger endpoints

### 3. Run the service

Load your environment, then start Uvicorn:

```bash
set -a
source .env
set +a
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Check the service:

```bash
curl http://localhost:8000/health
```

For production, put the application behind a TLS-terminating proxy. The service is not intended to be exposed directly over plain HTTP.

## Slack configuration

Configure the Slack app with:

- Slash command: `/promo`
- Interactivity enabled for the promo submission modal
- Event subscription request URL: `https://<host>/slack/events`
- Message events for the channels used by the request and approval workflows

The bot validates Slack requests through Slack Bolt using `SLACK_SIGNING_SECRET`. Restrict installation and channel access to the intended workspace.

## Tests

```bash
pytest -q
```

The suite covers parsing, regional routing, financial evaluation, approval classification, reminder behavior, reconciliation, and tracker compatibility.

This public snapshot has business-sensitive constants replaced with placeholders. Those replacements make a subset of economics assertions fail. Run the suite against the original private values, or replace the redacted fixtures with internally approved test fixtures, before treating a green test run as a release gate.

## Deployment

The repository includes:

- A production Dockerfile
- Render configuration
- A reusable Helm chart
- Production Kubernetes values
- GitHub Actions for dependency auditing, image builds, ECR publishing, and deployment-value updates
- A Kubernetes CronJob for reconciliation and reminder sweeps

Use runtime secrets from a secret manager. Do not pass credentials into Docker build arguments or bake environment files into images.

## Public snapshot

This repository is a sanitized public snapshot of an internal tool. Retailer and distributor names, routing maps, market economics, object IDs, and internal documentation have been replaced with placeholders. It is intended as a code and architecture sample, not a runnable deployment.

Before adapting it:

1. Supply your own retailer/region/distributor mappings through the lookups sheet or the `app/config.py` fallbacks.
2. Configure all IDs and credentials through environment variables. Never commit `.env` or a service-account key.
3. Use runtime secrets from a secret manager. Do not pass credentials into Docker build arguments or bake environment files into images.
4. Replace placeholder economics with your own values; a subset of the economics tests only passes against real values.
5. Run secret scanning and dependency auditing before any release.

## Design choices worth discussing

- Financial math is deterministic; the LLM is an optional parser/commentary layer, not the source of approval logic.
- Every evaluation exposes assumptions and data provenance instead of returning an opaque score.
- The workflow degrades to explicit fallbacks when integrations fail and records those fallbacks for reviewers.
- Approval authorization is checked against configured Slack user IDs.
- Reconciliation and idempotent reminder logic make the workflow resilient to pod restarts and duplicate scheduled runs.
