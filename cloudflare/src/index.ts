/**
 * Trading Bot auf Cloudflare.
 *
 * - Der Bot (Python, Docker-Image aus dem Projekt) laeuft als Cloudflare Container in der EU.
 * - Dieser Worker ist die einzige Tuer nach aussen: Anmeldung ueber eine eigene Seite (/login), danach ein
 *   signiertes Cookie fuer 30 Tage. Dann leitet er ans Dashboard im Container weiter. Der Container hat keine
 *   eigene oeffentliche Adresse. Basic-Auth geht weiter fuer Skripte, der Browser-Dialog erscheint aber nie.
 * - Ein Cron-Trigger fragt jede Minute nach dem Bot. Das startet ihn, falls Cloudflare ihn beendet hat,
 *   und verhindert, dass er wegen Untaetigkeit schlafen gelegt wird.
 * - Die Festplatte des Containers ist fluechtig. Der Bot schickt seinen Zustand an http://state.tradingbot/,
 *   dieser Worker faengt das ab und speichert es in R2. Von aussen ist dieser Speicher nicht erreichbar.
 */
import { Container, getContainer } from "@cloudflare/containers";
import { loginPage } from "./login";

export { ContainerProxy } from "@cloudflare/containers";

interface Env {
  BOT: DurableObjectNamespace<TradingBot>;
  STATE: R2Bucket;
  LOGIN_LIMIT?: RateLimit;
  DASHBOARD_USER?: string;
  DASHBOARD_PASSWORD?: string;
  // Aktien: Schluessel eines kostenlosen Alpaca-Papierkontos (nur fuer Kurse). npx wrangler secret put ALPACA_API_KEY
  ALPACA_API_KEY?: string;
  ALPACA_API_SECRET?: string;
  // KI-Konto: Sprachmodelle von Workers AI (10.000 Neurons am Tag kostenlos) oder OpenRouter (Schluessel als Secret)
  AI: Ai;
  OPENROUTER_API_KEY?: string;
}

const STATE_HOST = "state.tradingbot";
const AI_HOST = "ai.tradingbot";
const AI_MODEL = /^@cf\/[A-Za-z0-9_.\/-]{3,120}$/;
const BOT_PORT = 8765;
const SAFE_NAME = /^[A-Za-z0-9_.-]{1,128}$/;
const MAX_STATE_BYTES = 20 * 1024 * 1024;

/** Speichert und liefert Zustandsdateien des Bots in R2. Nur aus dem Container erreichbar. */
async function handleState(request: Request, env: Env): Promise<Response> {
  const name = decodeURIComponent(new URL(request.url).pathname.replace(/^\/+/, ""));
  if (!SAFE_NAME.test(name)) return new Response("ungueltiger Name", { status: 400 });
  if (request.method === "GET") {
    const obj = await env.STATE.get(name);
    if (!obj) return new Response("nicht vorhanden", { status: 404 });
    return new Response(obj.body, { headers: { "Content-Type": "application/json" } });
  }
  if (request.method === "PUT") {
    const body = await request.arrayBuffer();
    if (body.byteLength > MAX_STATE_BYTES) return new Response("zu gross", { status: 413 });
    await env.STATE.put(name, body, { httpMetadata: { contentType: "application/json" } });
    return new Response(null, { status: 204 });
  }
  return new Response("nicht erlaubt", { status: 405 });
}

/** Leitet eine KI-Anfrage des Bots an Workers AI weiter. Nur aus dem Container erreichbar. */
async function handleAi(request: Request, env: Env): Promise<Response> {
  if (request.method !== "POST") return new Response("nicht erlaubt", { status: 405 });
  let body: { model?: string; messages?: unknown; max_tokens?: number; response_format?: unknown };
  try {
    body = await request.json();
  } catch {
    return new Response("ungueltiges JSON", { status: 400 });
  }
  if (!body.model || !AI_MODEL.test(body.model) || !Array.isArray(body.messages)) {
    return new Response("model oder messages fehlen", { status: 400 });
  }
  try {
    const input: Record<string, unknown> = { messages: body.messages, max_tokens: Math.min(body.max_tokens ?? 600, 2000) };
    if (body.response_format) input.response_format = body.response_format;
    const result = await env.AI.run(body.model as keyof AiModels, input as never);
    return Response.json({ result });
  } catch (err) {
    return new Response(`Workers AI: ${String(err).slice(0, 300)}`, { status: 502 });
  }
}

export class TradingBot extends Container<Env> {
  defaultPort = BOT_PORT;
  // Der Cron-Trigger meldet sich jede Minute, deshalb schlaeft der Bot nie ein, solange der Worker laeuft.
  sleepAfter = "15m";
  enableInternet = true; // Kurse von der Boerse abrufen
  envVars: Record<string, string> = {
    TRADINGBOT_CONTAINER: "1",
    DASHBOARD_BIND: "0.0.0.0",
    TRADINGBOT_STATE_URL: `http://${STATE_HOST}/`,
    TRADINGBOT_AI_URL: `http://${AI_HOST}/`,
    TRADING_MODE: "paper",
    TZ: "Europe/Berlin",
  };

  constructor(ctx: DurableObjectState<{}>, env: Env) {
    super(ctx, env);
    // Secrets des Workers an den Bot weiterreichen (nur die, die er braucht)
    this.envVars = {
      ...this.envVars,
      ALPACA_API_KEY: env.ALPACA_API_KEY ?? "",
      ALPACA_API_SECRET: env.ALPACA_API_SECRET ?? "",
      OPENROUTER_API_KEY: env.OPENROUTER_API_KEY ?? "",
    };
  }

  override onStart(): void {
    console.log("Trading Bot gestartet");
  }

  override onStop(): void {
    console.log("Trading Bot beendet");
  }

  override onError(error: unknown): void {
    console.error("Trading Bot Fehler:", error);
    throw error;
  }
}

// Bewusst als Zuweisung und nicht als "static"-Feld: Nur der Setter der Bibliothek traegt den Handler in die
// Registry ein, die ContainerProxy beim Abfangen nutzt. Ein Klassenfeld wuerde den Setter umgehen.
TradingBot.outboundByHost = {
  [STATE_HOST]: (request: Request, env: Env) => handleState(request, env),
  [AI_HOST]: (request: Request, env: Env) => handleAi(request, env),
};

async function sha256(text: string): Promise<ArrayBuffer> {
  return crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
}

/** Vergleich in konstanter Zeit, damit die Antwortzeit nichts ueber das Passwort verraet. */
async function safeEqual(a: string, b: string): Promise<boolean> {
  const [ha, hb] = await Promise.all([sha256(a), sha256(b)]);
  return crypto.subtle.timingSafeEqual(ha, hb);
}

async function credentialsOk(env: Env, user: string, password: string): Promise<boolean> {
  if (!env.DASHBOARD_PASSWORD) return false; // ohne Passwort bleibt das Dashboard zu
  const userOk = await safeEqual(user, env.DASHBOARD_USER || "bot");
  const passOk = await safeEqual(password, env.DASHBOARD_PASSWORD);
  return userOk && passOk;
}

// ------------------------------------------------------------ Sitzung per Cookie
const COOKIE = "tb_session";
const SESSION_DAYS = 30;

async function hmac(env: Env, message: string): Promise<string> {
  // Schluessel haengt am Passwort: Wer das Passwort aendert, meldet alle Geraete ab.
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(`tradingbot-session:${env.DASHBOARD_USER || "bot"}:${env.DASHBOARD_PASSWORD}`),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message));
  return btoa(String.fromCharCode(...new Uint8Array(sig))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function newSession(env: Env): Promise<string> {
  const expires = Date.now() + SESSION_DAYS * 86400_000;
  return `v1.${expires}.${await hmac(env, `v1.${expires}`)}`;
}

function readCookie(request: Request, name: string): string | null {
  for (const part of (request.headers.get("Cookie") ?? "").split(";")) {
    const [k, ...rest] = part.trim().split("=");
    if (k === name) return rest.join("=");
  }
  return null;
}

async function sessionOk(request: Request, env: Env): Promise<boolean> {
  if (!env.DASHBOARD_PASSWORD) return false;
  const value = readCookie(request, COOKIE);
  if (!value) return false;
  const [version, expires, sig] = value.split(".");
  if (version !== "v1" || !expires || !sig || !(Number(expires) > Date.now())) return false;
  return safeEqual(sig, await hmac(env, `v1.${expires}`));
}

function sessionCookie(value: string, maxAge: number): string {
  return `${COOKIE}=${value}; Path=/; Max-Age=${maxAge}; HttpOnly; Secure; SameSite=Lax`;
}

async function basicOk(request: Request, env: Env): Promise<boolean> {
  const header = request.headers.get("Authorization") ?? "";
  if (!header.startsWith("Basic ")) return false;
  let decoded: string;
  try {
    decoded = new TextDecoder().decode(Uint8Array.from(atob(header.slice(6)), (c) => c.charCodeAt(0)));
  } catch {
    return false;
  }
  const sep = decoded.indexOf(":");
  if (sep < 0) return false;
  return credentialsOk(env, decoded.slice(0, sep), decoded.slice(sep + 1));
}

async function isAuthorized(request: Request, env: Env): Promise<boolean> {
  return (await sessionOk(request, env)) || (await basicOk(request, env));
}

async function tooManyAttempts(request: Request, env: Env): Promise<boolean> {
  if (!env.LOGIN_LIMIT) return false;
  const ip = request.headers.get("CF-Connecting-IP") ?? "unbekannt";
  const { success } = await env.LOGIN_LIMIT.limit({ key: ip });
  return !success;
}

const SECURITY_HEADERS: Record<string, string> = {
  "Strict-Transport-Security": "max-age=31536000",
  "X-Frame-Options": "DENY",
};

function plain(status: number, text: string, extra: Record<string, string> = {}): Response {
  return new Response(text, { status, headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store", ...SECURITY_HEADERS, ...extra } });
}

/**
 * Kommt eine schreibende Anfrage von dieser Seite selbst? Browser schicken meist Origin mit. Manche (z. B. Safari auf
 * dem iPhone bei Formularen) schicken "Origin: null" oder gar keins; dann zaehlt Sec-Fetch-Site, das der Browser
 * selbst setzt und keine fremde Seite faelschen kann. Ohne beides (z. B. curl) nur mit Basic-Auth.
 */
function sameOrigin(request: Request, url: URL): boolean {
  const origin = request.headers.get("Origin");
  if (origin && origin !== "null") return origin === url.origin;
  const site = request.headers.get("Sec-Fetch-Site");
  if (site) return site === "same-origin";
  return request.headers.has("Authorization");
}

function html(status: number, body: string): Response {
  return new Response(body, {
    status,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
      "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
      "Referrer-Policy": "same-origin",
      ...SECURITY_HEADERS,
    },
  });
}

function redirect(location: string, extra: Record<string, string> = {}): Response {
  return new Response(null, { status: 303, headers: { Location: location, "Cache-Control": "no-store", ...SECURITY_HEADERS, ...extra } });
}

/** /login und /logout. Gibt null zurueck, wenn die Anfrage etwas anderes ist. */
async function handleLogin(request: Request, env: Env, url: URL): Promise<Response | null> {
  if (url.pathname === "/logout") return redirect("/login", { "Set-Cookie": sessionCookie("", 0) });
  if (url.pathname !== "/login") return null;
  if (!env.DASHBOARD_PASSWORD) return html(503, loginPage("Kein Passwort gesetzt (wrangler secret put DASHBOARD_PASSWORD)."));
  if (request.method === "GET") {
    if (await sessionOk(request, env)) return redirect("/");
    return html(200, loginPage(null));
  }
  if (request.method !== "POST") return plain(405, "nicht erlaubt");
  if (!sameOrigin(request, url)) return plain(403, "fremde Herkunft");
  if (await tooManyAttempts(request, env)) return html(429, loginPage("Zu viele Versuche. Warte eine Minute."));
  const form = await request.formData().catch(() => null);
  const user = String(form?.get("user") ?? "").slice(0, 200);
  const password = String(form?.get("password") ?? "").slice(0, 500);
  if (await credentialsOk(env, user, password)) {
    return redirect("/", { "Set-Cookie": sessionCookie(await newSession(env), SESSION_DAYS * 86400) });
  }
  return html(401, loginPage("Benutzer oder Passwort stimmt nicht.", user));
}

// Ohne Anmeldung erreichbar: was der Browser fuer das Installieren als App braucht
const PUBLIC = new Set(["/manifest.webmanifest", "/static/icon.svg", "/static/icon-180.png", "/static/icon-192.png", "/static/icon-512.png"]);

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    const login = await handleLogin(request, env, url);
    if (login) return login;

    const isPublic = request.method === "GET" && PUBLIC.has(url.pathname);
    if (!isPublic && !(await isAuthorized(request, env))) {
      if (request.headers.has("Authorization") && (await tooManyAttempts(request, env))) {
        return plain(429, "Zu viele Anmeldeversuche, bitte eine Minute warten.");
      }
      // Seiten: zur Anmeldung. API: 401 ohne WWW-Authenticate, damit der Browser keinen eigenen Dialog zeigt.
      const wantsPage = request.method === "GET" && (request.headers.get("Accept") ?? "").includes("text/html");
      if (wantsPage) return redirect("/login");
      return new Response(JSON.stringify({ error: "Anmeldung abgelaufen", login: "/login" }), {
        status: 401,
        headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...SECURITY_HEADERS },
      });
    }

    const isWrite = request.method !== "GET" && request.method !== "HEAD";
    if (isWrite && !sameOrigin(request, url)) {
      return plain(403, "fremde Herkunft");
    }

    // Nur die noetigen Header weitergeben, Zugangsdaten bleiben im Worker
    const headers = new Headers();
    for (const name of ["Accept", "Content-Type", "X-Dashboard"]) {
      const value = request.headers.get(name);
      if (value !== null) headers.set(name, value);
    }
    if (isWrite) headers.set("Origin", "http://localhost"); // Herkunft oben schon geprueft
    const forwarded = new Request(`http://localhost:${BOT_PORT}${url.pathname}${url.search}`, {
      method: request.method,
      headers,
      body: isWrite ? await request.arrayBuffer() : undefined,
    });

    let response: Response;
    try {
      response = await getContainer(env.BOT).fetch(forwarded);
    } catch (error) {
      console.error("Container nicht erreichbar:", error);
      return plain(503, "Der Bot startet gerade. Bitte in einer Minute neu laden.", { "Retry-After": "60" });
    }
    const out = new Response(response.body, response);
    for (const [k, v] of Object.entries(SECURITY_HEADERS)) out.headers.set(k, v);
    return out;
  },

  async scheduled(_event: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(
      (async () => {
        try {
          const r = await getContainer(env.BOT).fetch(new Request(`http://localhost:${BOT_PORT}/api/health`));
          if (!r.ok) console.warn("Bot antwortet mit", r.status);
        } catch (error) {
          console.error("Bot nicht erreichbar, wird beim naechsten Aufruf neu gestartet:", error);
        }
      })(),
    );
  },
} satisfies ExportedHandler<Env>;
