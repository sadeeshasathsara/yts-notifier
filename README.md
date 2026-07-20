# YTS -> Telegram Notifier (Netlify Scheduled Function)

Automatically checks [YTS.gg](https://yts.gg) for new movie releases every 2 hours and sends Telegram notifications with movie details, IMDb rating, Rotten Tomatoes scores, and poster image.

Runs on **Netlify Free Tier** using **Netlify Scheduled Functions** and **Netlify Blobs** (no VPS required!).

---

## Features

- **Automated Timer**: Runs every 2 hours via Netlify Scheduled Functions.
- **Serverless State**: Uses Netlify Blobs to store seen movie IDs so you don't get duplicate notifications across runs.
- **Rich Telegram Messages**: Includes poster image, title in tap-to-copy Markdown code format, IMDb rating, and scraped Rotten Tomatoes (Tomatometer & Audience) scores.
- **100% Free Plan Compatible**: Uses ~360 function runs/month out of Netlify's 125,000 free monthly invocations.

---

## Setup & Deployment Instructions

### 1. Prerequisites
- A **Telegram Bot Token** (get one from [@BotFather](https://t.me/BotFather) on Telegram).
- Your **Telegram Chat ID** (or Channel ID).

---

### 2. Deploy to Netlify

#### Option A: Connect GitHub Repository to Netlify (Recommended)
1. Push this repository to GitHub.
2. Log into your [Netlify Dashboard](https://app.netlify.com).
3. Click **Add new site** > **Import an existing project**.
4. Select your GitHub repository.
5. Netlify will auto-detect `netlify.toml` and configure the build settings.
6. Click **Deploy Site**.

#### Option B: Deploy via Netlify CLI
```bash
npm install -g netlify-cli
netlify login
netlify init
netlify deploy --prod
```

---

### 3. Set Environment Variables on Netlify

In your Netlify Site Dashboard:
1. Go to **Site Configuration** > **Environment variables**.
2. Add the following **Required** variables:
   - `TELEGRAM_BOT_TOKEN`: `123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ`
   - `TELEGRAM_CHAT_ID`: `987654321` or `-1001234567890`

3. (Optional) Add filter variables:
   - `YTS_FILTER_QUALITY`: e.g. `1080p` or `2160p` (default: all)
   - `YTS_FILTER_GENRE`: e.g. `Action` or `Sci-Fi` (default: all)
   - `YTS_FILTER_MIN_RATING`: e.g. `7` (default: none)
   - `YTS_LIST_LIMIT`: e.g. `20` (default: 20)

---

### 4. How Netlify Blobs & Scheduling Work

- **Cron Schedule**: Defined in `netlify.toml` (`0 */2 * * *` = every 2 hours). Netlify triggers `netlify/functions/yts-notifier.js` automatically.
- **State Persistence**: The function uses `@netlify/blobs` to keep track of notified movie IDs (`seen_ids`) so no duplicate alerts are sent.

---

## Local Development & Testing

To test the function locally before deploying:

1. Install dependencies:
   ```bash
   npm install
   ```

2. Run Netlify Dev Server:
   ```bash
   npx netlify dev
   ```

3. Trigger the function endpoint manually at `http://localhost:8888/.netlify/functions/yts-notifier`.
