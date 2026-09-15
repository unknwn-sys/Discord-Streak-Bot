# Discord Streak Bot

A self-contained Discord bot that recreates the feeling of "streaks":
every member keeps a personal streak alive by sending at least one
message, image, or video anywhere in the server each day.

The bot lives inside a single permanent dashboard message with buttons.
Slash commands are not required for normal use.

Everything is contained in three files:

- `bot.py` — the entire bot
- `requirements.txt` — dependencies
- `README.md` — this file

Data is stored locally in a SQLite file (`streaks.db`) that the bot
creates automatically the first time it runs.

---

## 1. Requirements

- Python 3.11 or newer
- A Discord account with permission to create/manage a bot application
- A Discord server (guild) where you can invite the bot

## 2. Creating a Discord Developer Portal Application

1. Go to https://discord.com/developers/applications
2. Click **New Application**, give it a name (e.g. "Streaks"), and create it.

## 3. Creating the Bot

1. In your application, open the **Bot** tab.
2. Click **Add Bot** if it isn't already created.
3. Under **Privileged Gateway Intents**, enable **Message Content Intent**
   (see section 10 below for why this is required).

## 4. Getting the Bot Token

On the **Bot** tab, click **Reset Token** (or **Copy** if visible), and
copy the token. Keep this secret — never commit it or share it.

## 5. Getting the Application/Client ID

On the **General Information** tab, copy the **Application ID**. This is
your `CLIENT_ID`.

## 6. Getting the Guild/Server ID

1. In Discord, open **User Settings → Advanced** and enable **Developer
   Mode** (see section 9).
2. Right-click your server's icon in the server list and choose
   **Copy Server ID**. This is your `GUILD_ID`.

## 7. Getting the Dashboard Channel ID

1. Create (or pick) a text channel where the permanent public dashboard
   should live, e.g. `#streaks`.
2. Right-click the channel and choose **Copy Channel ID**. This is your
   `STREAK_DASHBOARD_CHANNEL_ID`.

## 8. Inviting the Bot

1. On the **OAuth2 → URL Generator** tab, select the `bot` and
   `applications.commands` scopes.
2. Under **Bot Permissions**, select at minimum:
   - View Channels
   - Send Messages
   - Embed Links
   - Read Message History
   - Use Application Commands
   Do **not** grant the bot the Administrator permission.
3. Open the generated URL and invite the bot to your server.

## 9. Enabling Developer Mode

**User Settings → Advanced → Developer Mode** (toggle on). This lets you
right-click servers, channels, and users to copy their IDs.

## 10. Enabling Message Content Intent

The bot needs **Message Content Intent** enabled in the Developer Portal
(Bot tab → Privileged Gateway Intents) because it inspects messages sent
anywhere in the server to detect qualifying activity (a plain message or
an image attachment) for the daily streak requirement. Without this
intent, the bot cannot see message content or attachments and streak
tracking will not work.

The bot does **not** require Presence Intent, and does not require the
Server Members Intent for its core functionality.

## 11. Required Bot Permissions

- View Channels
- Send Messages
- Embed Links
- Read Message History
- Use Application Commands

The bot should **not** be given Administrator permission.

## 12. Creating `.env`

Create a file named `.env` in the same folder as `bot.py`:

```
DISCORD_TOKEN=your_bot_token
CLIENT_ID=your_application_id
GUILD_ID=your_server_id
STREAK_DASHBOARD_CHANNEL_ID=your_dashboard_channel_id
```

Notes:

- The daily update/alert channel is **not** set in `.env`. It is
  configured later from the bot's admin settings UI and stored in the
  SQLite database (see section 16).
- Never commit `.env` to version control.

## 13. Installing Requirements

```
python -m venv venv
```

Windows:
```
venv\Scripts\activate
```

Linux/macOS:
```
source venv/bin/activate
```

Then install dependencies:
```
pip install -r requirements.txt
```

## 14. Running the Bot

```
python bot.py
```

On first run, the bot will:

- Create `streaks.db` automatically.
- Create all required tables.
- Post the permanent dashboard message in the configured dashboard
  channel.

## 15. How the Permanent Dashboard Works

The channel referenced by `STREAK_DASHBOARD_CHANNEL_ID` holds exactly
one persistent message with the public dashboard embed and buttons
(My Streak, Statistics, Achievements, Streak Freezes, How It Works,
Settings).

- On every startup, the bot looks up the stored dashboard message ID in
  SQLite and edits that existing message rather than creating a new
  one.
- If the stored message was deleted, the bot creates a new one and
  updates the stored ID.
- The dashboard itself never shows personal information — it is the
  same for every member. All personal information appears only in
  private (ephemeral) responses after clicking a button.
- The dashboard channel cannot be changed from the settings UI; it is
  controlled only by `STREAK_DASHBOARD_CHANNEL_ID` in `.env`.

## 16. Configuring the Daily Update Channel

The daily update/alert channel (where new-day, warning, streak-secured,
streak-broken, freeze, and milestone announcements are posted) is set
from the admin settings panel, not from `.env`:

1. Click **Settings** on the dashboard.
2. Choose **Server Settings** (only visible to members with the Manage
   Server permission).
3. Click **Change Channel** and pick a channel from the dropdown.

The selected channel ID is stored in SQLite and persists across
restarts.

## 17. How Daily Activity Works

A member's daily requirement is satisfied by any one of:

- A normal text message anywhere in the server.
- A message with an image attachment anywhere in the server.
- A message with a video attachment anywhere in the server.

Any combination of text, images, and videos in the same message still
only counts once, and multiple qualifying messages in a day still only
count once. The following never
count: bot messages, webhook messages, reactions, button interactions,
and system messages. Members do not need to post in any specific
channel — activity anywhere in the guild counts.

The bot stays quiet during normal activity; it does not post a public
notification every time someone sends a qualifying message.

## 18. How Streaks Work

- The streak "day" rolls over at the configured deadline time (default
  midnight) in the configured timezone (default `Asia/Karachi`), not
  necessarily at the calendar date boundary if a custom deadline is
  set.
- At the deadline, the bot evaluates the day that just ended for every
  tracked member:
  - Completed the requirement → streak increases by one.
  - Missed the requirement but has a freeze available → a freeze is
    consumed automatically and the streak is preserved.
  - Missed the requirement with no freeze available → the streak
    resets to zero.
- Evaluation is idempotent: if the bot restarts or the scheduler ticks
  more than once around the deadline, a day is only ever evaluated
  once, so freezes are never double-consumed and streaks are never
  double-counted.
- Milestones (3, 7, 14, 30, 50, 100, 365 days) are announced once each,
  the first time a member's streak reaches them.

## 19. How Streak Freezes Work

- Every member receives 3 streak freezes per calendar month.
- A freeze is never activated manually — it is applied automatically
  during the daily evaluation if a member missed the requirement and
  still has a freeze balance remaining.
- The balance never exceeds 3 and cannot go negative.

## 20. How Monthly Reset Works

Freeze balances are tracked per member per calendar month
(`YYYY-MM`). When a new month begins, the bot creates a fresh balance
of 3 for that month the first time it is needed — there is nothing to
configure, and unused freezes from the previous month do not carry
over or stack.

## 21. How Administrators Configure the Bot

From the dashboard, click **Settings → Server Settings** (requires the
Manage Server permission), or use the optional `/streak-admin` slash
command. From there you can:

- Change the daily updates channel.
- Change the timezone (any valid IANA timezone name, e.g.
  `America/New_York`).
- Change the daily deadline (24-hour `HH:MM`).
- Toggle the 2-hour reminder.
- Toggle the 30-minute reminder.
- Enable or disable the streak system entirely.

Non-administrators only see personal settings and cannot change server
configuration.

## 22. Data Retention

- Detailed daily activity records (which day a member sent a qualifying
  message, image, or video) are retained for approximately the last 3
  months. Rows older than that are deleted automatically once a day.
- Streak counters are **not** derived from those detailed records. Each
  member's `current_streak`, `longest_streak`, `started_at`
  (`streak_start_date`), `last_completed_date`, and lifetime statistics
  (total completed days, total streaks, broken streaks, freezes used)
  are stored as their own persistent values in the database and are
  updated by one increment/decrement at a time as each day is
  evaluated.
- Because of this, deleting old detailed activity rows has no effect on
  an active streak. A streak that has run for months or years keeps its
  full count and start date even after the underlying day-by-day
  evidence for its earliest days has been cleaned up. A streak can
  continue indefinitely without the bot ever needing to retain years of
  raw activity rows.
- Message content and image contents are never stored — only the fact
  that a qualifying activity happened on a given day, for a given
  member, is recorded, and only for the retention window described
  above.
- Achievements, freeze balances, freeze usage history, guild
  configuration, and the dashboard message record are permanent and are
  never touched by this cleanup.
- You can verify this behavior yourself with the built-in self-test,
  which creates a disposable temporary database (never your real
  `streaks.db`), sets up a 258-day streak with an old activity record,
  runs cleanup, and confirms the streak is still 258 (and becomes 259
  after the next successful day):
  ```
  python bot.py --self-test-cleanup
  ```

## 23. Troubleshooting

**The bot doesn't post the dashboard.**
Check that `STREAK_DASHBOARD_CHANNEL_ID` is correct and that the bot
has View Channel and Send Messages permissions in that channel. Check
the console logs for a permissions or "channel not found" error.

**Daily updates never appear.**
Make sure a daily updates channel has been selected in
Settings → Server Settings → Change Channel, and that the bot can post
in that channel.

**Message activity isn't being detected.**
Confirm Message Content Intent is enabled in the Developer Portal (Bot
tab) and that the bot was restarted after enabling it.

**"Invalid timezone" when changing timezone.**
Use a valid IANA timezone name such as `Asia/Karachi`,
`Europe/London`, or `America/New_York` — not an abbreviation or UTC
offset.

**Duplicate dashboard messages appeared.**
This can happen if the bot's stored message ID pointed at a message
that was deleted while the bot was offline; the bot will create a
replacement. Delete any extra dashboard messages manually if needed —
the bot only tracks and edits one at a time going forward.

**I restarted the bot near the deadline — did anything get missed or
duplicated?**
No. The daily evaluation is guarded by a unique record per server per
day in the database, so it will run exactly once for any given day
regardless of restarts or scheduler timing.

**Buttons stopped responding after a bot restart.**
The dashboard buttons use fixed component IDs and are re-registered on
every startup, so they should keep working immediately after a
restart. If they don't, confirm the bot process actually restarted
successfully and check the logs for startup errors.
