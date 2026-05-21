# Telegram Job Pull Bot

Personal Telegram-only vacancy review bot for Odesa restaurant/cafe roles.

The active bot flow scans configured Telegram channels, adds matching vacancies to a persistent SQLite review queue, and lets one owner review vacancies one by one.

## Active Profile

The bot queues only these target roles:

- waiter / waitress / официант / официантка / офіціант / офіціантка
- runner / раннер / ранер / помощник официанта / помічник офіціанта / помощник в зал / помічник у зал
- hostess / хостес

Restaurant/cafe words are context only. They do not pass the filter without one of the target roles.

Hard rejects are applied to the relevant role section where possible:

- 18+ requirements, unless the text clearly allows 17-year-olds
- female-only requirements
- required experience such as `досвід роботи від 1 року` or `опыт обязателен`

Soft experience wording such as `досвід бажаний`, `желательно с опытом`, or `буде перевагою` is allowed.

## Commands And Buttons

Run `/start` to show the persistent keyboard:

- `🔎 Pull Telegram jobs`
- `🧾 Review vacancies`
- `❤️ Saved vacancies`
- `🗑 Rejected / Debug`

Slash commands:

- `/pull_tg` — scan Telegram channels for the last 48 hours only
- `/review` — continue reviewing pending vacancies
- `/saved`, `/liked`, `/latest` — open saved vacancies
- `/rejected` — inspect recently rejected vacancies
- `/last_report` — show the latest detailed Telegram pull report
- `/settings` — show current filter settings

Website job pulling is disabled in the active bot. There is no Website Jobs button, no website command in the bot menu, and `sites.yaml` is empty.

## Telegram Pull

`/pull_tg` and the Telegram pull button scan every channel in `channels.txt` from newest messages backward until messages are older than 48 hours.

By default there is no per-channel message cap:

```yaml
telegram_scan_max_messages_per_channel: 0
```

After a pull, the bot sends only a short result:

```text
Added X vacancies to review queue.
```

or:

```text
No new vacancies found.
```

Detailed counters are available only through `/last_report`.

## Review Queue

Matching vacancies are inserted into SQLite with `review_state = pending`.

Review mode shows one vacancy at a time:

```text
🧾 Vacancy review
Vacancies left: X

Text:
<full original Telegram message text>

Link:
<source link>
```

Reply keyboard buttons under the input field:

- `✅ Like` marks the vacancy as liked/saved
- `❌ Dislike` marks it as disliked/reviewed
- `🚪 Exit` returns to the persistent main menu

After either action, the next pending vacancy is shown immediately. Pending, liked, disliked, and deleted-saved states survive bot restarts.

Original Telegram message text is stored in SQLite and displayed without summarizing or rewriting. If a message is too long for Telegram, the bot splits it into multiple messages without shortening it.

## Duplicate Tracking

Telegram dedupe is based on source identity plus a normalized content hash:

- source type: `telegram`
- normalized Telegram message link or channel/chat identity plus Telegram message id
- normalized text/content hash for reposts and linkless messages

The same vacancy is not queued again after another pull, even after restart. Liked, disliked, deleted, rejected, and cross-channel reposted items remain known so they do not reappear.

## Saved Vacancies

Saved mode shows a compact numbered list:

```text
❤️ Saved vacancies

#1 — Mangal Meat House
https://t.me/channel/123

#2 — No info
https://t.me/channel/456
```

Buttons:

- `🔍 Open by number`
- `🗑 Delete by number`
- `⬅️ Previous`
- `➡️ Next`
- `🚪 Exit`

Opening by number shows the full original vacancy text and link. Deleting marks the saved row as `deleted`; it does not remove duplicate tracking.

To clear old vacancy, queue, saved, rejected and stats rows without deleting the database schema or config, run:

```powershell
python scripts\reset_vacancy_state.py
```

## Setup

Create `.env`:

```env
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_BOT_TOKEN=123456:bot_token
MY_TELEGRAM_USER_ID=123456789
```

Install and run:

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

On the first run, Telethon may ask for your phone number, login code and 2FA password. After login it creates `telegram_user.session`.

## Telegram Channels

Edit `channels.txt`, one channel per line:

```text
@v_odesse5
@rabota_odessa
```

If a channel is private, your Telegram account must be a member.
