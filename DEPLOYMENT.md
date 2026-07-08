# Online Deployment

This project is ready for a mixed local/cloud setup:

- Streamlit Community Cloud runs the web UI.
- Neon Postgres stores shared history.
- GitHub Actions can run scheduled price updates.
- The local app can read and write the same Neon data when `DATABASE_URL` is present in `.env`.

## Accounts Needed

1. GitHub account and repository: already done.
2. Neon account and database: already done.
3. Streamlit Community Cloud account: use GitHub login at `share.streamlit.io`.

No extra paid account is required for the first online test.

## Deploy Streamlit App

1. Commit and push the latest code to GitHub.
2. Open Streamlit Community Cloud.
3. Click `Create app`.
4. Choose repository `SerzhTime/cs2-basket-tool`.
5. Set branch to `main`.
6. Set main file path to `app.py`.
7. Open `Advanced settings`.
8. Paste secrets using `.streamlit/secrets.toml.example` as the variable-name checklist.
9. Deploy.

Important: paste actual values only in Streamlit Cloud secrets. Do not commit `.streamlit/secrets.toml`.

## Required Streamlit Secrets

At minimum, the online app needs:

- `APP_PASSWORD`
- `DATABASE_URL`
- `HALOSKINS_LOWEST_PRICE_URL`
- API keys for every API-backed market you want enabled online.

Use the same values you already added to GitHub Actions secrets.

`APP_PASSWORD` protects the Streamlit page itself. If it is unset, the app opens normally.

## Scheduled Updates

The online Streamlit app shows data. GitHub Actions creates automatic snapshots.

After one manual GitHub Actions run creates a good snapshot:

1. Open `.github/workflows/scheduled-price-update.yml`.
2. Uncomment the `schedule` block.
3. Commit and push.

The app will show new snapshots from Neon automatically. If the app page is already open, refresh it or wait for the cache TTL.

## Local and Online Sync

- Online update -> writes to Neon -> local app sees it immediately only if local `.env` uses Postgres mode.
- Local update in SQLite mode -> writes to local SQLite -> online app sees it after you click `Sync Neon` and refresh/cache expires.
- If local `.env` has `DATABASE_BACKEND=sqlite`, the local app is fast and local-first. `Sync Neon` pushes local snapshots to Neon, then pulls Neon-only snapshots back into local SQLite.
- Sync matches snapshots by timestamp. If the same timestamp exists locally and in Neon, local SQLite is treated as the source of truth.

## Safety Checks

Before enabling schedule:

1. Run GitHub Actions manually.
2. Confirm a new snapshot appears in Neon/history graph.
3. Check obvious outliers in the table.
4. Only then enable the cron schedule.
