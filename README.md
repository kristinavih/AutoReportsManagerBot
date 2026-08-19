# AutoReportsManagerBot

Internal Telegram-based operations and reporting automation system.

## Overview

AutoReportsManagerBot was designed and built independently from idea to working product to automate recurring team operations, reporting and data handling.

The project combines Telegram workflows, Google Sheets, data processing, role-based logic, scheduled reporting and lead/statistics handling in one system.

## What the system does

- Collects recurring reports from team members
- Sends scheduled reminders and follow-ups
- Aggregates operational statistics
- Works with lead data from multiple sources
- Calculates and prepares Revenue / Profit / ROI reporting
- Synchronizes data with Google Sheets
- Supports role-based user logic
- Normalizes and validates incoming data
- Tracks report status and missing submissions
- Produces consolidated Telegram reports
- Handles recurring scheduled tasks
- Uses a database and migrations for persistent data
- Can be deployed in Docker

## My role

I designed and implemented the complete project independently:

- defined the product logic and user scenarios
- decomposed requirements into technical tasks
- designed the data flow and system behavior
- implemented the code with an AI-assisted development workflow
- created and configured Google Sheets used by the system
- connected users and role logic
- built reporting and statistics flows
- tested real workflows and edge cases
- debugged failures and data inconsistencies
- deployed and iteratively improved the system

## AI-assisted development

AI tools were used as development assistants throughout the project for:

- implementation
- refactoring
- debugging
- architecture discussions
- error analysis
- feature development
- test-case thinking

All product decisions, workflow logic, requirements, validation and final testing were handled by me.

## Tech stack

- Python
- Telegram Bot API
- Telethon
- Google Sheets / Google APIs
- Google Apps Script
- SQL database
- Alembic
- Docker
- API integrations
- AI-assisted development

## Repository structure

```text
.
├── alembic/               # Database migrations
├── db/                    # Database models and helpers
├── bot.py                 # Main bot logic
├── buyer_leads.py         # Lead-related workflows
├── buyer_statistics.py    # Statistics and reporting logic
├── telethon_leads.py      # Telegram source integration
├── Dockerfile
├── alembic.ini
├── requirements.txt
├── .env.example
└── .gitignore
```

## Configuration

Create a local `.env` file based on `.env.example` and provide your own credentials.

Example:

```env
BOT_TOKEN=
FORM_URL=
REPORT_SHEET_ID=
GOOGLE_JSON=credentials.json

API_ID=
API_HASH=
TELETHON_SESSION_STRING=
LEADS_SOURCE_CHAT_ID=

DATABASE_URL=
```

Real credentials, user identifiers, internal links and production data are intentionally excluded from this portfolio repository.

## Security note

This is a sanitized portfolio version of a production-oriented internal tool. Sensitive business data, credentials and private identifiers are not included.

## Status

Portfolio / demonstration version based on a real operational automation project.
