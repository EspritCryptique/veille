name: Ingestion Telegram

on:
  schedule:
    - cron: "*/5 * * * *"   # toutes les 5 minutes
  workflow_dispatch: {}      # permet aussi de lancer à la main (bouton "Run workflow")

jobs:
  ingest:
    runs-on: ubuntu-latest
    steps:
      - name: Récupérer le code
        uses: actions/checkout@v4

      - name: Installer Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Installer les dépendances
        run: pip install -r requirements.txt

      - name: Lancer l'ingestion
        env:
          TELEGRAM_API_ID: ${{ secrets.TELEGRAM_API_ID }}
          TELEGRAM_API_HASH: ${{ secrets.TELEGRAM_API_HASH }}
          TELEGRAM_SESSION: ${{ secrets.TELEGRAM_SESSION }}
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_KEY: ${{ secrets.SUPABASE_KEY }}
        run: python ingest.py
