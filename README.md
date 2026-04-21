# Job Application Agent
Autonomous job application agent using NVIDIA NIM free API.
No human review loop. Hourly email status digest.

## Free LLM Stack
| Model | Use | Cost |
|---|---|---|
| `moonshotai/kimi-k2-thinking` | Cover letters | FREE (40 req/min) |
| `stepfun-ai/step-3.5-flash` | Fit scoring | FREE (40 req/min) |
| `z-ai/glm4.7` | Fallback | FREE (40 req/min) |

API: https://integrate.api.nvidia.com/v1
Key: https://build.nvidia.com/settings/api-keys

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt
playwright install chromium

# 2. Get free NVIDIA NIM API key
# Visit: https://build.nvidia.com/settings/api-keys
# Sign up — free, 40 req/min, no credit card

# 3. Configure
cp config.yaml config.yaml   # edit config.yaml with your details

# 4. Gmail App Password (for email digests)
# Google Account > Security > 2FA > App Passwords
# Create one for "Mail" > copy the 16-char password into config.yaml

# 5. Add your resume
# Paste plain text resume into resume.txt
# Place resume.pdf in the project root

# 6. Run
python agent.py
```

## Email Digest (hourly)
Styled like Sentinel Quant status reports:
- Applied count
- Low-fit skipped count
- Error count
- Avg fit score
- Recent activity table (company, role, fit, status)
- LLM model info

## How it works
1. JobFinder scrapes Indeed UK + Reed + direct career URLs every 30 min
2. JobScraper fetches the full JD for each URL
3. FitScorer uses step-3.5-flash to score fit 0-100
4. Jobs below min_fit_score (default 58) are skipped
5. CoverLetterGenerator uses kimi-k2-thinking to write tailored letter
6. FormFiller uses Playwright to fill and submit the form (Greenhouse/Lever/Workday/generic)
7. All activity logged to applications.db (SQLite)
8. Hourly email digest sent via SMTP

## Project Structure
```
job-agent/
├── agent.py           # Main loop
├── config.yaml        # All settings
├── resume.txt         # Plain text resume (LLM context)
├── resume.pdf         # PDF resume (form upload)
├── requirements.txt
├── applications.db    # SQLite (auto-created)
├── agent.log          # Log file (auto-created)
├── core/
│   ├── llm.py         # NVIDIA NIM client
│   ├── fit_scorer.py  # LLM fit scoring
│   ├── cover_letter.py# LLM cover letter generation
│   ├── form_filler.py # Playwright form filler
│   └── notifier.py    # Hourly email digest
└── scrapers/
    ├── scraper.py     # JD scraper
    └── finder.py      # Job discovery (Indeed/Reed/direct)
```

## Monitoring
```bash
# Watch live
tail -f agent.log

# Check applications
sqlite3 applications.db "SELECT company,title,fit_score,status,applied_at FROM applications ORDER BY applied_at DESC LIMIT 20;"

# Check skipped
sqlite3 applications.db "SELECT url,reason FROM skipped ORDER BY ts DESC LIMIT 20;"

# Stats
sqlite3 applications.db "SELECT status,COUNT(*) FROM applications GROUP BY status;"
```
