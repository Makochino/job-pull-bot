# Telegram Job Pull Bot

Personal Telegram bot for manually collecting restaurant/cafe/hospitality vacancies in Odesa from Telegram channels and job websites.

The project uses `aiogram 3.x`, `Telethon`, `SQLite`, `requests + BeautifulSoup`, `config.yaml`, `channels.txt`, `sites.yaml`, and `.env`.

No Docker, web dashboard, Redis, Celery, FastAPI, Django or PostgreSQL.

## Current Profile

Current profile: restaurant/cafe jobs in Odesa.

The filter is strict. A vacancy must contain at least one restaurant/cafe/hospitality keyword:

- waiter / waitress
- официант / офіціант
- runner / раннер / ранер
- hostess / хостес
- bartender / бармен
- barista / бариста
- помощник кухни / помічник кухаря
- restaurant / cafe / ресторан / кафе

Generic terms such as `без опыта`, `без досвіду`, `no experience`, `part-time`, `подработка`, `стажер`, `trainee`, `intern`, `Одесса`, `Odesa` are only bonuses. They never pass the filter by themselves.

The bot rejects office jobs, casino/gambling, crypto/investment, suspicious high-income posts, generic no-experience jobs, and female-only vacancies.

## Commands And Buttons

Run `/start` to show persistent buttons under the Telegram text input field:

- `🔎 Telegram jobs`
- `🌐 Website jobs`
- `📌 Latest`
- `📊 Stats`
- `⚙️ Settings`
- `❓ Help`

Telegram bots cannot completely remove the text input field. A bot can only show a persistent reply keyboard below it. The keyboard is restored on normal bot responses, reports, settings, stats, latest, help and unknown messages.

Slash commands still work:

- `/start` — show welcome and buttons
- `/help` — short help
- `/pull_tg` — search Telegram channels
- `/pull_sites` — search websites
- `/latest` — latest saved vacancies
- `/stats` — statistics
- `/settings` — current filters
- `/pull` — explains that Telegram and website search are separate

If you send any unknown message, the bot replies:

```text
Use the buttons below to control the bot.
```

and attaches the persistent keyboard again.

## Startup Notification

By default the bot sends a short online message to `MY_TELEGRAM_USER_ID` when it starts:

```yaml
notify_user_on_startup: true
```

Message:

```text
✅ Job Pull Bot is online

Use the buttons below to control the bot.
```

This also restores the persistent keyboard after a restart. Telegram still requires that you have opened or messaged the bot at least once before the bot can message you.

Disable it:

```yaml
notify_user_on_startup: false
```

If Telegram refuses the startup message, the bot logs the error and keeps running.

## Telegram Search Mode

By default `/pull_tg` and the `🔎 Telegram jobs` button send only new matching Telegram vacancies that were never sent before:

```yaml
telegram_resend_latest_on_pull: false
telegram_latest_limit: 10
```

The bot still:

- applies the strict restaurant/cafe filter
- rejects female-only and scam-like jobs
- removes duplicates inside the same pull
- removes cross-channel duplicates inside the same pull
- saves/updates database rows where possible

`telegram_latest_limit` is kept in config, but it is only used when resend mode is enabled.

To resend the latest matching Telegram posts on every pull, explicitly enable:

```yaml
telegram_resend_latest_on_pull: true
telegram_latest_limit: 10
```

Website search also sends only new website vacancies by default because website listings are more static.

## Website Search Reports

Website reports separate the important counters:

- websites checked
- cards found
- parsed cards
- matched by filter
- detail pages fetched
- duplicates
- already sent
- new sendable
- sent now
- errors

If matching website vacancies exist but all of them were already sent or duplicates, the bot says:

```text
✅ No new website vacancies.

All matching vacancies were already sent or duplicated.
```

If zero vacancies matched the filter, the bot says:

```text
😕 No matching website vacancies found.
```

This avoids confusing “Matched: 62 / Sent now: 0” reports.

## Batch Sending

Search results are sent in batches:

```yaml
batch_size: 5
```

If more vacancies are available, the bot shows inline buttons:

- `Next 5`
- `Stop`

Only pagination uses inline buttons. Main controls stay in the persistent bottom keyboard.

A vacancy is marked as `sent` only after it is actually sent. If you press `Stop`, remaining vacancies are not marked as sent.

## Auto-delete Messages

By default, bot messages and your command/button messages are deleted after 10 minutes:

```yaml
auto_delete_messages_after_seconds: 600
```

Disable auto-delete:

```yaml
auto_delete_messages_after_seconds: 0
```

or:

```yaml
auto_delete_messages_after_seconds: null
```

Deleting old messages does not intentionally remove the persistent keyboard. The next bot response restores it.

## Text Cleaning

The bot now separates display cleaning from duplicate normalization:

- `clean_text_for_display()` keeps readable line breaks, salary lines, schedules, phones, addresses, bullet lists and Telegram usernames.
- `normalize_text_for_hashing()` aggressively normalizes text for duplicate hashes and similarity checks.

Telegram vacancy messages preserve employer formatting as much as possible. Only excessive repeated symbols, duplicate lines and more than two empty lines in a row are cleaned.

## Website Vacancy Details

For website vacancies, the bot parses listing pages and then opens individual vacancy detail pages when a link is available.

Website messages include only:

- relevance
- title
- salary
- schedule / working hours
- workplace / address / location
- phone
- link

No short description and no huge preview paragraph are sent.

If salary, phone, schedule or address is hidden by the website or not present in static HTML, the bot shows:

```text
not specified
```

Detail fetching is limited and delayed:

```yaml
website_detail_pages_limit: 20
website_detail_delay_seconds: 0.7
```

If the detail limit is reached, the debug report shows how many detail pages were skipped by the limit.

## Robota.ua Limitation

Robota.ua may be dynamic or bot-protected. It can return HTTP 200 with a short static HTML shell and 0 vacancy cards for `requests + BeautifulSoup`.

This is not fatal. The debug report shows:

```text
Robota.ua waiter Odesa: 0 cards — likely dynamic/bot-protected page
```

Work.ua is kept as the reliable static source. Playwright is intentionally not included to keep the bot simple and stable.

## Setup

Create a bot with `@BotFather` and copy the bot token.

Get `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from:

```text
https://my.telegram.org
```

Find your numeric Telegram user id with `@userinfobot` or `@RawDataBot`.

Create `.env`:

```bash
cp .env.example .env
```

Fill it:

```env
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_BOT_TOKEN=123456:bot_token
MY_TELEGRAM_USER_ID=123456789
```

The bot only answers `MY_TELEGRAM_USER_ID`.

## Run On Windows

```powershell
cd job_pull_bot
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

On the first run, Telethon may ask for your phone number, login code and 2FA password. After login it creates `telegram_user.session`.

## Run On Linux VPS

```bash
git clone <repo_url>
cd job_pull_bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python main.py
```

First run should be interactive so Telethon can authorize your Telegram account.

## Systemd Example

```bash
sudo nano /etc/systemd/system/job-pull-bot.service
```

```ini
[Unit]
Description=Telegram Job Pull Bot
After=network.target

[Service]
WorkingDirectory=/home/ubuntu/job_pull_bot
ExecStart=/home/ubuntu/job_pull_bot/venv/bin/python /home/ubuntu/job_pull_bot/main.py
Restart=always
RestartSec=10
User=ubuntu
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable job-pull-bot
sudo systemctl start job-pull-bot
sudo systemctl status job-pull-bot
journalctl -u job-pull-bot -f
```

## Telegram Channels

Edit `channels.txt`. One channel per line:

```text
@workua_jobs
@odessa_work
```

If a channel is private, your Telegram account must be a member.

## Websites

Edit `sites.yaml`.

The parser supports:

- `url`
- `urls`
- `pages`
- `base_url`
- selector lists for `vacancy_selector`, `title_selector`, `link_selector`, `description_selector`
- optional per-site `headers`

The current `sites.yaml` contains restaurant/cafe searches for Work.ua and one Robota.ua URL per query type.

## Adjust Filtering

Edit `config.yaml`.

Important groups:

- `core_keywords` — required restaurant/cafe/hospitality terms
- `restaurant_context_keywords` — restaurant/cafe context bonuses
- `bonus_keywords` — bonuses such as Odesa, schedule, salary, no experience
- `hard_reject_keywords` — immediate rejects
- `female_only_reject_patterns` — explicit female-only wording
- `scam_reject_patterns` — extra scam-like patterns

If filtering is too strict:

1. Lower `min_score` from `5` to `4`.
2. Add missing restaurant/cafe terms to `core_keywords`.
3. Add useful context words to `restaurant_context_keywords`.

If filtering is too broad:

1. Keep `min_score: 5` or raise it to `6`.
2. Add unwanted phrases to `hard_reject_keywords`.
3. Add explicit female-only phrases to `female_only_reject_patterns`.

## Useful Config

```yaml
telegram_resend_latest_on_pull: false
telegram_latest_limit: 10
notify_user_on_startup: true
auto_delete_messages_after_seconds: 600
batch_size: 5
max_results_per_pull: 20
debug_parsing: true
website_detail_pages_limit: 20
website_detail_delay_seconds: 0.7
```
