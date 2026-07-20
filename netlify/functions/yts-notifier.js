import { getStore } from "@netlify/blobs";

// --------------------------------------------------------------------------
// Configuration
// --------------------------------------------------------------------------
const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN || "";
const TELEGRAM_CHAT_ID = process.env.TELEGRAM_CHAT_ID || "";

const YTS_API_BASE = process.env.YTS_API_BASE || "https://movies-api.accel.li/api/v2";
const LIST_LIMIT = parseInt(process.env.YTS_LIST_LIMIT || "20", 10);

const FILTER_QUALITY = process.env.YTS_FILTER_QUALITY || "";
const FILTER_GENRE = process.env.YTS_FILTER_GENRE || "";
const FILTER_MIN_RATING = process.env.YTS_FILTER_MIN_RATING || "";

const USER_AGENT = "yts-telegram-notifier/1.0 (Netlify Serverless Function)";

// --------------------------------------------------------------------------
// Telegram MarkdownV2 Helper
// --------------------------------------------------------------------------
function escapeMarkdownV2(text) {
  if (!text) return "";
  return text.replace(/[_*\[\]()~`>#+\-=|{}.!]/g, "\\$&");
}

function decodeHTMLEntities(text) {
  if (!text) return "";
  return text
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#039;/g, "'")
    .replace(/&#39;/g, "'");
}

// --------------------------------------------------------------------------
// Rotten Tomatoes Scraper
// --------------------------------------------------------------------------
function visibleText(html) {
  if (!html) return "";
  // Strip <script> and <style> content
  const noScripts = html.replace(/<(script|style)[^>]*>[\s\S]*?<\/\1>/gi, " ");
  // Strip tags
  const stripped = noScripts.replace(/<[^>]+>/g, " ");
  // Unescape & collapse whitespace
  return decodeHTMLEntities(stripped).replace(/\s+/g, " ");
}

async function fetchRTScores(movieUrl) {
  if (!movieUrl) return { tomatometer: null, audience: null };
  try {
    const resp = await fetch(movieUrl, {
      headers: { "User-Agent": USER_AGENT },
    });
    if (!resp.ok) return { tomatometer: null, audience: null };
    const html = await resp.text();
    const text = visibleText(html);

    const tomatoMatch = text.match(/(\d{1,3})\s*%\s*TOMATOMETER/i);
    const audienceMatch = text.match(/(\d{1,3})\s*%\s*AUDIENCE/i);

    return {
      tomatometer: tomatoMatch ? `${tomatoMatch[1]}%` : null,
      audience: audienceMatch ? `${audienceMatch[1]}%` : null,
    };
  } catch (err) {
    console.warn(`Could not fetch RT scores for ${movieUrl}:`, err.message);
    return { tomatometer: null, audience: null };
  }
}

// --------------------------------------------------------------------------
// Telegram API
// --------------------------------------------------------------------------
function buildCaption(movie, tomatometer, audience) {
  const rawTitle = decodeHTMLEntities(movie.title || "Unknown title");
  const year = movie.year || "";
  const genres = (movie.genres || []).join(", ");
  const imdb = movie.rating;

  const titleLine = `\`${escapeMarkdownV2(rawTitle)}\``;
  const genreLine = genres ? `🎭 ${escapeMarkdownV2(genres)}` : "";
  const dateAndGenre = `📅 ${year}` + (genreLine ? `   ${genreLine}` : "");
  const imdbLine = imdb ? `⭐ IMDb: *${imdb}/10*` : "⭐ IMDb: N/A";
  const rtLine = `🍅 Tomatometer: *${tomatometer || "N/A"}*  |  🍿 Audience: *${audience || "N/A"}*`;
  const urlLine = `[View on YTS](${escapeMarkdownV2(movie.url || "")})`;

  return [titleLine, dateAndGenre, imdbLine, rtLine, "", urlLine].join("\n");
}

async function sendTelegramNotification(posterUrl, caption) {
  const telegramBaseUrl = `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}`;

  if (posterUrl) {
    const endpoint = `${telegramBaseUrl}/sendPhoto`;
    const resp = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: TELEGRAM_CHAT_ID,
        photo: posterUrl,
        caption: caption,
        parse_mode: "MarkdownV2",
      }),
    });
    if (!resp.ok) {
      const errText = await resp.text();
      throw new Error(`Telegram sendPhoto failed (${resp.status}): ${errText}`);
    }
  } else {
    const endpoint = `${telegramBaseUrl}/sendMessage`;
    const resp = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: TELEGRAM_CHAT_ID,
        text: caption,
        parse_mode: "MarkdownV2",
      }),
    });
    if (!resp.ok) {
      const errText = await resp.text();
      throw new Error(`Telegram sendMessage failed (${resp.status}): ${errText}`);
    }
  }
}

// --------------------------------------------------------------------------
// YTS API Fetcher
// --------------------------------------------------------------------------
async function fetchLatestMovies() {
  const params = new URLSearchParams({
    limit: LIST_LIMIT.toString(),
    sort_by: "date_added",
    order_by: "desc",
    with_rt_ratings: "true",
  });

  if (FILTER_QUALITY && FILTER_QUALITY.toLowerCase() !== "all") {
    params.append("quality", FILTER_QUALITY);
  }
  if (FILTER_GENRE && FILTER_GENRE.toLowerCase() !== "all") {
    params.append("genre", FILTER_GENRE);
  }
  if (FILTER_MIN_RATING) {
    params.append("minimum_rating", FILTER_MIN_RATING);
  }

  const url = `${YTS_API_BASE}/list_movies.json?${params.toString()}`;
  const resp = await fetch(url, {
    headers: { "User-Agent": USER_AGENT },
  });

  if (!resp.ok) {
    throw new Error(`YTS API returned status ${resp.status}`);
  }

  const data = await resp.json();
  if (data.status !== "ok") {
    throw new Error(`YTS API error: ${data.status_message}`);
  }

  return data.data?.movies || [];
}

// Helper to pause execution between Telegram calls
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// --------------------------------------------------------------------------
// Netlify Scheduled Function Handler
// --------------------------------------------------------------------------
export default async function handler(req, context) {
  console.log("yts-notifier function triggered");

  if (!TELEGRAM_BOT_TOKEN || !TELEGRAM_CHAT_ID) {
    console.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variables.");
    return new Response("Configuration Error: Missing Telegram Bot credentials", { status: 500 });
  }

  try {
    // 1. Initialize Netlify Blobs store
    const store = getStore("yts-notifier");
    let seenArray = (await store.get("seen_ids", { type: "json" })) || [];
    const seenSet = new Set(seenArray.map(String));

    // 2. Fetch latest movies from YTS
    const movies = await fetchLatestMovies();
    console.log(`Fetched ${movies.length} movies from YTS API.`);

    // 3. Filter for unseen movies
    const newMovies = movies.filter((m) => !seenSet.has(String(m.id)));

    // Process oldest first so Telegram chat reads chronologically
    newMovies.reverse();

    if (newMovies.length === 0) {
      console.log("No new movies found.");
      return new Response(JSON.stringify({ message: "No new movies found." }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    console.log(`Found ${newMovies.length} new movie(s) to notify.`);

    // 4. Send notifications for new movies
    let notifiedCount = 0;
    for (const movie of newMovies) {
      const movieId = String(movie.id);
      const title = movie.title || "Unknown";
      const poster =
        movie.large_cover_image || movie.medium_cover_image || movie.small_cover_image || null;

      const { tomatometer, audience } = await fetchRTScores(movie.url);
      const caption = buildCaption(movie, tomatometer, audience);

      try {
        await sendTelegramNotification(poster, caption);
        console.log(`Notified: ${title} (${movieId})`);
        seenSet.add(movieId);
        notifiedCount++;

        // Pause briefly to respect Telegram rate limits
        await sleep(1500);
      } catch (err) {
        console.error(`Failed to notify for ${title} (${movieId}):`, err.message);
        // Do not mark as seen so it retries next run
      }
    }

    // 5. Update Blobs state (keep max 2000 IDs)
    const updatedSeenArray = Array.from(seenSet).slice(-2000);
    await store.setJSON("seen_ids", updatedSeenArray);
    console.log(`Updated state file in Netlify Blobs. Total seen IDs: ${updatedSeenArray.length}`);

    return new Response(
      JSON.stringify({
        message: `Successfully processed ${newMovies.length} new movies. Notified ${notifiedCount}.`,
      }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  } catch (error) {
    console.error("Error during scheduled run:", error);
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500,
      headers: { "Content-Type": "application/json" },
    });
  }
}
