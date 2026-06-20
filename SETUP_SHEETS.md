# One-time setup: live Google Sheet dashboard

The engine appends to a Google Sheet on every run and trade (Dashboard + Trades + Runs tabs).
It stays disabled and harmless until you complete these steps. ~10 minutes.

1. **Google Cloud project**: open https://console.cloud.google.com → create or pick a project.

2. **Enable the API**: APIs & Services → Library → search "Google Sheets API" → **Enable**.

3. **Create a service account**: IAM & Admin → Service Accounts → **Create service account**.
   Name it `ftmo-operator`. No roles needed → Done.

4. **Download its key**: open the service account → **Keys** → Add Key → **Create new key** → **JSON**
   → download. Move the file to `~/trading/secrets/ftmo-sheets-sa.json`:
   `mkdir -p ~/trading/secrets && mv ~/Downloads/<that-file>.json ~/trading/secrets/ftmo-sheets-sa.json && chmod 600 ~/trading/secrets/ftmo-sheets-sa.json`

5. **Copy the service-account email** — it's the `client_email` inside that JSON, looks like
   `ftmo-operator@<project>.iam.gserviceaccount.com`.

6. **Create the Sheet**: go to https://sheets.new, name it "FTMO Operator Dashboard".
   Click **Share**, paste the service-account email, give it **Editor**, send.

7. **Copy the Sheet ID** from its URL — the long string between `/d/` and `/edit`:
   `https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`

8. **Put both in `~/trading/.env`**:
   ```
   GOOGLE_SA_JSON=/Users/marwanghostine/trading/secrets/ftmo-sheets-sa.json
   GSHEET_ID=<the id from step 7>
   ```

9. **Tell me "sheets ready"** — I'll run `engine.cli audit`, which auto-creates the three tabs and
   writes the first dashboard + run row so we can confirm it live.

Security: the JSON key is a credential — keep it in `~/trading/secrets/` (chmod 600), never share it.
The service account can only touch Sheets you explicitly shared with its email.
