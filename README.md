# Humorous Review Scout

Automated discovery and curation of humorous reviews for a weekly shortlist.

## What This Does
- Discovers places from Google Maps (SerpApi Google Maps API)
- Collects recent, low-rating reviews
- Scores humor potential and flags safety risks
- Deduplicates similar reviews and builds a curated shortlist
- Tracks review lifecycle states (`new`, `selected`, `used`, `discarded`)

## Requirements
- Python 3.9+ recommended
- `pip` available in your environment
- SerpApi API key for discovery and review collection
- OpenAI API key optional (only if you enable LLM scoring)
- Playwright browsers optional (only if you enable screenshots)

## Setup
1. Copy `config.example.yaml` to `config.yaml` and edit locations/categories.
2. Set required environment variables:
   - `SERPAPI_API_KEY`
   - `OPENAI_API_KEY` (optional, only for LLM scoring)
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. If you want screenshots, install Playwright browsers:
   ```bash
   python -m playwright install
   ```

## Usage
Run the full weekly pipeline:
```bash
python -m humor_reviews.run weekly
```

Outputs are written to `out/`:
- `weekly_shortlist_YYYY-MM-DD.json`
- `weekly_shortlist_YYYY-MM-DD.md`

### Individual pipeline steps
- `python -m humor_reviews.run discover`
- `python -m humor_reviews.run collect`
- `python -m humor_reviews.run shortlist`

### Manual review management
- Add a place:
  ```bash
  python -m humor_reviews.run add-place <place_id>
  ```
- Update review status:
  ```bash
  python -m humor_reviews.run set-status <review_id> <new|selected|used|discarded>
  ```

## Config UI
Launch the local configuration UI:
```bash
python3 scripts/config_ui.py
```
Then open `http://127.0.0.1:5173` in your browser.

## LLM Scoring Test
```bash
python3 scripts/test_score.py
```
This reads `OPENAI_API_KEY` from `.env` if present.

## Notes
- SerpApi has quotas and rate limits. The pipeline throttles calls.
- Reviews are fetched via SerpApi Google Maps Reviews API using `data_id` from local results.
- Humor scoring is heuristic with an optional LLM hook (see `humor_reviews/humor.py`).
- Humor scoring softly boosts likely Spanish reviews via a language heuristic (configurable bonus).
