# Cloud IP Probe

Use this before deploying the full app online. It tests whether APIs and price pages work from a cloud/datacenter IP without writing to the app database or price history.

## Local Test

From the app folder:

```powershell
.\.venv\Scripts\python.exe scripts\cloud_probe.py --items 3 --markdown reports\local-cloud-probe.md --json reports\local-cloud-probe.json
```

Local results show your current network behavior. They are useful as a baseline, but they do not prove cloud compatibility.

## GitHub Actions Test

1. Push this app folder to a GitHub repository.
2. In GitHub, open `Settings -> Secrets and variables -> Actions`.
3. Add only the secrets you want to test:
   - `HALOSKINS_LOWEST_PRICE_URL` if your HaloSkins key is already embedded in the full API link
   - `HALOSKINS_API_KEY` only if you want the app to build the HaloSkins URL from the key
   - `CSFLOAT_API_KEY`
   - `C5GAME_APP_KEY`
   - `EXCHANGERATE_USD_LATEST_URL`
   - `DMARKET_PUBLIC_KEY`
   - `DMARKET_SECRET_KEY`
   - `MARKETCSGO_API_KEY`
   - `WAXPEER_API_KEY`
4. Open `Actions -> Cloud IP Probe -> Run workflow`.
5. Download the `cloud-probe-report` artifact after the run.

The workflow is manual by default. The schedule block is commented out so it will not run automatically until you intentionally enable it.

The probe normally exits successfully even when some sources are blocked, so the report artifact is easy to download. Add `--fail-on-bad` only if you want CI to fail when any source is blocked, errored, or unusable.

## How To Read Results

- `ok`: endpoint/page is reachable and returned a usable API result or price-like page text.
- `skipped`: credentials are not configured for that API.
- `blocked`: HTTP status or page text suggests Cloudflare, captcha, login, rate limit, or access denial.
- `unusable`: request succeeded, but the response did not contain price-like data.
- `error`: network, timeout, server, parser, or adapter exception.

For scraped pages, `HTTP 200` is not enough. A page is only useful if `price` is `yes` and `block` is `no`.

## What To Compare

Run the same probe locally and in GitHub Actions:

- If local is `ok` but GitHub is `blocked`, that source does not tolerate GitHub cloud IPs.
- If both are `ok`, it is a reasonable candidate for online scheduled updates.
- If API sources work but page sources fail, deploy online updates for API-backed markets first and keep scraped repairs local.
