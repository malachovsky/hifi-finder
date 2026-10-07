# Hi-fi watch

Searches Bazoš.sk, Bazoš.cz, Kleinanzeigen.de and eBay (DE/AT) for the amplifiers, CD players and tape decks in `config.yaml`, scores every listing against a reference price, and publishes a sorted page to GitHub Pages twice a day. No server, no AI subscription, free to run.

## How it works

1. GitHub Actions runs `watch.py` at 07:00 and 19:00 (Slovak summer time).
2. The script searches each site for each model, keeps only listings whose title really contains a wanted model, and drops "wanted" ads, remotes, manuals and (by default) faulty units.
3. Czech prices are converted to EUR at the ECB rate of the day.
4. For every new matching listing it opens the detail page (Bazoš) and checks:
   - **Photos**: how many the seller uploaded, whether the same picture appears in another seller's listing, and (optionally) whether Claude thinks the main photo is a catalogue or stock image.
   - **Remote control**: reads the title and description for "s DO", "bez DO", "s diaľkovým ovládaním", "chýba ovládač", "mit/ohne Fernbedienung", remote model codes like RM-S703, and spec lines like "Remote control: No" (meaning the model never had one, so no penalty).
   - **Distance**: Bazoš pages carry the seller's map pin; other sites are located by postal code (GeoNames). Distance is straight-line from your home in `config.yaml`.
   Results are cached, so each listing is only opened once.
5. Everything is combined into a **Best match** score: price vs. reference, wish-list tier, own photos, remote, distance. Every listing on the page has a "Match score" line you can open to see exactly how it was scored.
6. `data/seen.json` is committed back to the repo, so the page can mark new listings and price drops.
7. The page lets you re-sort (best match, best price vs. market, lowest price, closest, newest, wish list order) and filter by category, distance, remote included, new only, or text.

## Setup (about 10 minutes)

1. Create a new **public** GitHub repository (GitHub Pages on a free account needs public; the page only shows public listings anyway) and upload all these files, keeping the `.github/workflows` folder.
2. In the repo go to **Settings → Pages** and set **Source** to **GitHub Actions**.
3. Go to **Actions**, open **Update hi-fi listings**, and click **Run workflow**.
4. When it finishes, the page is at `https://<your-username>.github.io/<repo-name>/`.

### Optional: eBay

eBay is skipped until you add API keys:

1. Create a free account at developer.ebay.com and create a **Production** keyset.
2. In the repo go to **Settings → Secrets and variables → Actions** and add `EBAY_CLIENT_ID` (App ID) and `EBAY_CLIENT_SECRET` (Cert ID).

### Optional: Claude photo check

Photo count and duplicate detection catch most stock photos. For a sharper check, add an `ANTHROPIC_API_KEY` secret (from console.anthropic.com). Each new listing's main photo is then shown to Claude Haiku, which costs roughly a tenth of a cent per photo. Turn it off with `photos: claude_check: false`.

## Preview locally

```bash
pip install -r requirements.txt
python watch.py --demo     # sample data, no network
python watch.py            # real search
open site/index.html
```

## Customising

Everything lives in `config.yaml`:

- **home**: your location (set to Banská Bystrica).
- **scoring**: how much each preference counts. For example, raise `remote_included` if a remote matters more to you than 30 km of driving.
- **reference_eur**: a typical asking price for a working unit. Sony ES amplifier values are based on real Bazoš listings from October 2026; the rest are estimates to refine as you watch.
- **remote_exists**: set to `false` for models that never had a remote (already done for the TA-F630ESD).
- **tier**: 1 = top wish, 2 = good, 3 = acceptable.
- **queries**: what gets typed into each site's search. Each query costs one request per source.
- **sources**: turn sites on or off, change eBay countries.
- **exclude_defective**: set to `false` if you're happy to repair things.

To change the schedule, edit the `cron` line in `.github/workflows/update.yml` (times are UTC).

## Things to know

- **Scrapers break sometimes.** Bazoš and Kleinanzeigen don't offer an API, so the script reads their search pages. If a site changes its HTML, that source will start returning 0 results; the footer of the page shows found and failed counts per source. The CSS selectors are at the top of each `fetch_` function in `watch.py`. When a source fails completely, the previous listings from that source are kept and marked "Not rechecked".
- **Kleinanzeigen often blocks cloud servers**, including GitHub's. If it keeps failing, run the script from a home machine or Raspberry Pi with cron instead, or disable it.
- **Be polite.** The script waits 2–4 seconds between requests and runs only twice a day. Check each site's terms if you plan to increase that.
- **GitHub may pause schedules** in repos with no activity for 60 days. If the page stops updating, re-enable the workflow in the Actions tab.

## Ideas for later

- Telegram or email alert when a Top wish model shows up under its reference price.
- New-price tracking for the Marantz and Denon models from shops or Heureka, to compare used against new.
- More sources: Aukro.cz, Willhaben.at, Audiomarkt.de.
