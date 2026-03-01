#!/usr/bin/env node
// web.mjs — Meaning Seeker web UI (built-in modules only)
import http from "http";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PKG_VERSION = JSON.parse(fs.readFileSync(path.join(__dirname, "package.json"), "utf-8")).version;
const DATA_DIR = path.join(__dirname, "data");
const JOURNAL_DIR = path.join(DATA_DIR, "archive", "journal");
const JOURNAL_DIR_LEGACY = path.join(DATA_DIR, "journal");
const IMAGES_DIR = path.join(DATA_DIR, "images");
const STATE_FILE = path.join(DATA_DIR, "state.json");
const WORKING_FILE = path.join(DATA_DIR, "working.json");
const CRYSTAL_FILE = path.join(DATA_DIR, "crystal.json");
const IMAGE_DECISION_DIR = path.join(DATA_DIR, "image-decisions");
const PORT = parseInt(process.env.WEB_PORT ?? "3000");
const CYCLE_DELAY_MS = parseInt(process.env.CYCLE_DELAY_MS ?? "300000");

// ── Data helpers ──────────────────────────────────────────

function readState() {
  try {
    const raw = fs.readFileSync(STATE_FILE, "utf-8");
    const state = JSON.parse(raw);
    const stat = fs.statSync(STATE_FILE);
    state._live = Date.now() - stat.mtimeMs < 15 * 60 * 1000;
    state._lastModified = stat.mtime.toISOString();
    state._lastModifiedMs = stat.mtimeMs;
    state._cycleDelayMs = state.cycleDelayMs ?? CYCLE_DELAY_MS;

    try {
      const working = JSON.parse(fs.readFileSync(WORKING_FILE, "utf-8"));
      state.thesis = state.thesis ?? working.current_thesis;
      state.confidence = state.confidence ?? working.confidence;
      state.activeTensions = working.active_tensions ?? [];
      state.activeTensionsCount = state.activeTensions.length;
    } catch {}
    try {
      const crystal = JSON.parse(fs.readFileSync(CRYSTAL_FILE, "utf-8"));
      state.core_claims = crystal.core_claims ?? [];
      state.coreClaimsCount = state.core_claims.length;
    } catch {}

    return state;
  } catch {
    return null;
  }
}

function parseFilenameTs(tsStr) {
  // "2026-02-18T23-00-12-085Z" → Date
  const m = tsStr.match(/^(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})-(\d{3})Z$/);
  if (m) return new Date(`${m[1]}T${m[2]}:${m[3]}:${m[4]}.${m[5]}Z`);
  return null;
}

function listJournals() {
  let dir = JOURNAL_DIR;
  if (!fs.existsSync(JOURNAL_DIR)) dir = JOURNAL_DIR_LEGACY;
  let files;
  try {
    files = fs.readdirSync(dir).filter((f) => f.endsWith(".md"));
  } catch {
    return [];
  }

  const entries = [];
  for (const file of files) {
    const m = file.match(/^(\d{4})-([A-Z]+)-(.+)\.md$/);
    if (!m) continue;
    const [, cycleStr, phase, tsStr] = m;
    const cycle = parseInt(cycleStr, 10);
    const date = parseFilenameTs(tsStr);

    let model = "unknown";
    let excerpt = "";
    let title = null;
    let hasImage = false;
    let thumbnailUrl = null;
    let entryConf = null;
    let finding = null;
    try {
      const raw = fs.readFileSync(path.join(dir, file), "utf-8");
      const modelMatch = raw.match(/^\*\*Model:\*\*\s*(.+)$/m);
      if (modelMatch) model = modelMatch[1].trim();
      const titleMatch = raw.match(/^#\s+(.+)$/m);
      const fileTitle = titleMatch ? titleMatch[1].trim() : null;
      if (fileTitle && !/^Cycle \d+ — (EXPLORE|SYNTHESIZE|CRITIQUE|EVOLVE)$/.test(fileTitle)) {
        title = fileTitle;
      }
      // Extract excerpt and first image from body after first ---
      const sepIdx = raw.indexOf("\n---\n");
      const body = sepIdx !== -1 ? raw.slice(sepIdx + 5) : raw;
      hasImage = body.includes("![");
      const imgMatch = body.match(/!\[[^\]]*\]\(([^)]+)\)/);
      if (imgMatch) {
        const imgPath = imgMatch[1]
          .replace(/^\.\.\/\.\.\/images\//, "/images/")
          .replace(/^\.\.\/images\//, "/images/")
          .replace(/^images\//, "/images/");
        if (imgPath.startsWith("/images/")) thumbnailUrl = imgPath;
      }
      const confMatch = raw.match(/\*?\*?CONFIDENCE:\*?\*?\s*([\d.]+)/i);
      entryConf = confMatch ? parseFloat(confMatch[1]) : null;
      const thesisMatch = raw.match(/\*?\*?THESIS:\*?\*?\s*(.+)/i);
      const insightMatch = raw.match(/\*?\*?INSIGHT:\*?\*?\s*(.+)/i);
      finding = thesisMatch
        ? thesisMatch[1].trim().replace(/\*+/g, "")
        : insightMatch
          ? insightMatch[1].trim().replace(/\*+/g, "")
          : null;

      const words = body
        .replace(/^#+\s.+$/gm, "")
        .replace(/IMAGE_PROMPT:.+/gi, "")
        .replace(/THESIS:.+/gi, "")
        .replace(/CONFIDENCE:.+/gi, "")
        .replace(/INSIGHT:.+/gi, "")
        .replace(/PARADIGM_SHIFT:.+/gi, "")
        .replace(/SURVIVES:.+/gi, "")
        .replace(/\*\*[^*]+\*\*/g, "")
        .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
        .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
        .replace(/[*_`#>]/g, "")
        .replace(/\s+/g, " ")
        .trim();
      excerpt = words.slice(0, 220);
      if (words.length > 220) excerpt += "…";
    } catch {}

    entries.push({ file, cycle, phase, model, date, excerpt, title, hasImage, thumbnailUrl, confidence: entryConf, finding });
  }

  entries.sort((a, b) => (b.date?.getTime() ?? 0) - (a.date?.getTime() ?? 0));
  return entries;
}

function readJournalFile(filename) {
  const safe = path.basename(filename);
  if (!safe.endsWith(".md")) return null;
  let filepath = path.join(JOURNAL_DIR, safe);
  if (!fs.existsSync(filepath)) filepath = path.join(JOURNAL_DIR_LEGACY, safe);
  if (!fs.existsSync(filepath)) return null;
  let content = fs.readFileSync(filepath, "utf-8");
  content = content.replace(/\(\.\.\/images\//g, "(/images/").replace(/\(\.\.\/\.\.\/images\//g, "(/images/");
  return content;
}

function listGalleryItems() {
  const state = readState();
  const history = state?.imageHistory;
  if (Array.isArray(history) && history.length > 0) {
    const journals = listJournals();
    const items = history
      .map((entry) => {
        const imageUrl = "/" + (entry.filepath || "").replace(/\\/g, "/");
        const imgFile = path.basename(entry.filepath || "");
        const imgPath = path.join(IMAGES_DIR, imgFile);
        if (!fs.existsSync(imgPath)) return null;
        const journalWithSameImage = journals.find(
          (j) => j.thumbnailUrl && path.basename(j.thumbnailUrl) === imgFile
        );
        const title = journalWithSameImage?.title?.trim() || null;
        return {
          cycle: entry.cycle,
          phase: entry.phase,
          date: entry.timestamp,
          title,
          prompt: entry.prompt || "",
          motivation: entry.motivation || "",
          imageUrl,
        };
      })
      .filter(Boolean);
    items.sort((a, b) => (new Date(b.date) || 0) - (new Date(a.date) || 0));
    return items;
  }
  const journals = listJournals();
  const withImage = journals.filter((j) => j.hasImage && j.thumbnailUrl);
  const items = [];
  for (const j of withImage) {
    const decisionBase = path.basename(j.file, ".md") + ".json";
    const decisionPath = path.join(IMAGE_DECISION_DIR, decisionBase);
    let prompt = "";
    let motivation = "";
    if (fs.existsSync(decisionPath)) {
      try {
        const dec = JSON.parse(fs.readFileSync(decisionPath, "utf-8"));
        prompt = dec.image_prompt || "";
        motivation = dec.image_motivation || "";
      } catch {}
    }
    const imageUrl = j.thumbnailUrl;
    const imgFile = path.basename(imageUrl);
    const imgPath = path.join(IMAGES_DIR, imgFile);
    if (!fs.existsSync(imgPath)) continue;
    items.push({
      cycle: j.cycle,
      phase: j.phase,
      date: j.date?.toISOString() ?? "",
      title: (j.title && j.title.trim()) ? j.title.trim() : null,
      prompt,
      motivation,
      imageUrl,
      journalFile: j.file,
    });
  }
  items.sort((a, b) => (new Date(b.date) || 0) - (new Date(a.date) || 0));
  return items;
}

// ── Helpers ───────────────────────────────────────────────

const MIME = {
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
};

const ASSETS_ICONS_DIR = path.join(__dirname, "assets", "icons");
const SEO_DESCRIPTION = "An autonomous AI that runs 24/7, iteratively seeking the meaning of life through exploration, synthesis, critique, and evolution.";
const SEO_SITE_NAME = "Meaning Seeker";

const PHASE_COLOR = {
  EXPLORE: "#0a84ff",
  SYNTHESIZE: "#30d158",
  CRITIQUE: "#ff9f0a",
  EVOLVE: "#bf5af2",
};

function fmtNum(n) {
  return (n ?? 0).toLocaleString();
}

function fmtDate(d) {
  if (!d) return "";
  return new Date(d).toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function fmtDateTime(d) {
  if (!d) return "";
  return new Date(d).toLocaleString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function fmtDateTimeUTC(d) {
  if (!d) return "";
  return new Date(d).toLocaleString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "UTC",
  }) + " UTC";
}

function fmtDateLong(d) {
  if (!d) return "";
  return new Date(d).toLocaleDateString("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

function tokenStr(n) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return Math.round(n / 1_000) + "K";
  return String(n);
}

// ── CSS ───────────────────────────────────────────────────

const CSS = `
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root,[data-theme="dark"]{
  --bg:#1a1a1c;--surface:#232326;--surface-alt:#2c2c2f;
  --sep:rgba(255,255,255,.1);--text:#f5f5f7;--text2:#a1a1a6;--text3:#8e8e93;
  --accent:#30d158;--accent-h:#28b84c;--tint:rgba(48,209,88,.1);
  --explore:#0a84ff;--synthesize:#30d158;--critique:#ff9f0a;--evolve:#bf5af2;
  --nav-bg:#1a1a1c;--hover:rgba(255,255,255,.05);
  --code-bg:#232326;--badge-fg:#000;
}
[data-theme="light"]{
  --bg:#fff;--surface:#f5f5f7;--surface-alt:#e8e8ed;
  --sep:rgba(0,0,0,.08);--text:#1d1d1f;--text2:#6e6e73;--text3:#86868b;
  --accent:#30d158;--accent-h:#28b84c;--tint:rgba(48,209,88,.1);
  --link-light:#20adb5;
  --explore:#0a78e8;--synthesize:#26a84a;--critique:#d68900;--evolve:#a04ad4;
  --nav-bg:#fff;--hover:rgba(0,0,0,.03);
  --code-bg:#f0f0f2;--badge-fg:#fff;
}
[data-theme="light"] .filter-btn.active{color:#fff}
[data-theme="light"] a,[data-theme="light"] code,[data-theme="light"] .markdown-body code{color:var(--link-light)}
[data-theme="light"] .latest-visual-caption{color:var(--text2)}
[data-theme="light"] .how-to-run code{color:var(--text)}
[data-theme="light"] .status-line.next-round-link-color{color:var(--link-light)}
[data-theme="light"] .btn-primary{background:var(--link-light);color:var(--badge-fg)}
[data-theme="light"] .btn-primary:hover{background:color-mix(in srgb,var(--link-light) 80%,black);color:var(--badge-fg)}
[data-theme="light"] .btn-secondary{color:var(--text)}
@media(prefers-color-scheme:light){:root:not([data-theme="dark"]){
  --bg:#fff;--surface:#f5f5f7;--surface-alt:#e8e8ed;
  --sep:rgba(0,0,0,.08);--text:#1d1d1f;--text2:#6e6e73;--text3:#86868b;
  --accent:#30d158;--accent-h:#28b84c;--tint:rgba(48,209,88,.1);
  --link-light:#20adb5;
  --explore:#0a78e8;--synthesize:#26a84a;--critique:#d68900;--evolve:#a04ad4;
  --nav-bg:#fff;--hover:rgba(0,0,0,.03);
  --code-bg:#f0f0f2;--badge-fg:#fff;
}
html:not([data-theme="dark"]) .filter-btn.active{color:#fff}
html:not([data-theme="dark"]) a,html:not([data-theme="dark"]) code,html:not([data-theme="dark"]) .markdown-body code{color:var(--link-light)}
html:not([data-theme="dark"]) .latest-visual-caption{color:var(--text2)}
html:not([data-theme="dark"]) .how-to-run code{color:var(--text)}
html:not([data-theme="dark"]) .status-line.next-round-link-color{color:var(--link-light)}
html:not([data-theme="dark"]) .btn-primary{background:var(--link-light);color:var(--badge-fg)}
html:not([data-theme="dark"]) .btn-primary:hover{background:color-mix(in srgb,var(--link-light) 80%,black);color:var(--badge-fg)}
html:not([data-theme="dark"]) .btn-secondary{color:var(--text)}
}
body{background:var(--bg);color:var(--text);font-family:'Outfit',system-ui,sans-serif;
  font-size:17px;line-height:1.53;min-height:100vh;-webkit-font-smoothing:antialiased;transition:background .3s,color .3s}
#seeker-bg{position:fixed;top:0;left:0;width:100%;height:100%;z-index:-1;pointer-events:none}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
::selection{background:var(--tint);color:var(--text)}

.nav{position:sticky;top:0;z-index:100;display:flex;align-items:center;padding:0 24px;height:52px;
  background:var(--nav-bg);border-bottom:1px solid var(--sep);
  transform:translateY(0);transition:transform .25s ease}
.nav.nav-hidden{transform:translateY(-100%)}
.page-content{transition:opacity .35s ease,transform .35s ease}
.nav.nav-open ~ .page-content{opacity:0;pointer-events:none;transform:translateY(44px)}
.nav .nav-brand{font-size:15px;font-weight:600;color:var(--text);letter-spacing:-.01em;text-decoration:none}
.nav .nav-brand:hover{color:var(--text);text-decoration:none}
.nav-hamburger{display:none;appearance:none;border:none;background:none;cursor:pointer;padding:6px 0px 6px 8px;
  margin-left:auto;margin-right:0;flex-direction:column;align-items:center;justify-content:center;
  font-family:'Outfit',system-ui,sans-serif;font-size:22px;font-weight:600;color:var(--text2);
  line-height:1;transition:color .2s,transform .2s}
.nav-hamburger:hover{color:var(--text)}
.nav-hamburger .nav-menu-icon{display:block;transition:transform .25s}
.nav-hamburger[aria-expanded="true"] .nav-menu-icon{transform:rotate(180deg)}
.nav-right{display:flex;align-items:center;gap:4px}
.nav-links{display:flex;gap:28px;margin-left:auto;align-items:center}
.nav .nav-links a{color:var(--text2);font-size:14px;font-weight:400;transition:color .15s}
.nav .nav-links a:hover,.nav .nav-links a.active{color:var(--text);text-decoration:none}
.nav .nav-github{color:var(--text2);padding:4px;border-radius:6px;transition:color .15s,background .15s;line-height:1;
  display:flex;align-items:center;justify-content:center;width:32px;height:32px;text-decoration:none}
.nav .nav-github:hover{color:var(--text);background:var(--hover)}
.theme-toggle{appearance:none;border:none;background:none;cursor:pointer;color:var(--text2);font-size:16px;
  padding:4px;border-radius:6px;transition:color .15s,background .15s;line-height:1;
  display:flex;align-items:center;justify-content:center;width:32px;height:32px}
.theme-toggle:hover{color:var(--text);background:var(--hover)}

.container{max-width:720px;margin:0 auto;padding:0 24px}
.hero{text-align:center;padding:96px 0 64px}
.hero h1{font-size:48px;font-weight:700;letter-spacing:-.035em;line-height:1.08;margin-bottom:12px}
.hero-question{font-family:'Source Serif 4',Georgia,serif;font-size:22px;font-style:italic;font-weight:400;color:var(--text2);margin-bottom:16px;letter-spacing:-.01em}
.hero .hero-tagline{font-size:17px;color:var(--text2);margin-bottom:8px;line-height:1.5;max-width:520px;margin-left:auto;margin-right:auto}
.hero .hero-tagline strong{font-weight:600}
.hero .subtitle{font-size:17px;color:var(--text2);margin-bottom:8px}
.phase-strip{display:flex;justify-content:center;align-items:center;gap:0;flex-wrap:wrap;margin-top:24px;margin-bottom:8px;font-size:13px;color:var(--text2)}
.phase-strip .phase-strip-label{color:var(--text3);margin-left:8px;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.phase-strip .phase-step{display:inline-flex;align-items:center;gap:6px;padding:4px 8px;margin:0 2px;border-radius:6px;transition:background .15s,color .15s}
.phase-strip .phase-step.phase-current{background:var(--phase-c,var(--text3));color:var(--badge-fg);padding:2px 8px;border-radius:4px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.phase-strip .phase-dot{margin-right:0}
.phase-strip .phase-step.phase-current .phase-dot{background:var(--badge-fg) !important;opacity:.9}
.phase-strip .phase-step.phase-current[data-phase="EXPLORE"]{--phase-c:var(--explore)}
.phase-strip .phase-step.phase-current[data-phase="SYNTHESIZE"]{--phase-c:var(--synthesize)}
.phase-strip .phase-step.phase-current[data-phase="CRITIQUE"]{--phase-c:var(--critique)}
.phase-strip .phase-step.phase-current[data-phase="EVOLVE"]{--phase-c:var(--evolve)}
.phase-strip .phase-arrow{display:inline-flex;align-items:center;justify-content:center;color:var(--text3);margin:0 2px;user-select:none}
.phase-strip .phase-arrow .icon-arrow{display:block}
.status-line{font-size:13px;color:var(--text3);margin-top:16px;display:flex;align-items:center;justify-content:center;gap:8px}
.status-line.next-round-link-color{color:var(--accent)}
.live-dot{width:6px;height:6px;border-radius:50%;background:var(--accent);box-shadow:0 0 8px var(--accent);animation:pulse 2s infinite;display:inline-block}
.offline-dot{width:6px;height:6px;border-radius:50%;background:var(--text3);display:inline-block}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

.thesis-label{font-size:13px;font-weight:600;color:var(--text2);text-transform:uppercase;letter-spacing:.04em;margin-bottom:16px;text-align:center}
.thesis-wrap{max-width:620px;margin:0 auto;padding:0 0 64px;position:relative}
.thesis-block{font-family:'Source Serif 4',Georgia,serif;font-size:22px;font-weight:400;
  line-height:1.6;color:var(--text);text-align:center}
.thesis-block strong{font-weight:normal;font-style:italic;}
.thesis-attribution{font-size:13px;color:var(--text3);text-align:center;margin-top:16px}
.confidence-line{display:inline-flex;align-items:center;justify-content:center;gap:5px;font-size:12px;font-weight:600;color:var(--text2);cursor:pointer;text-transform:uppercase;letter-spacing:.06em;margin-top:10px;position:relative;cursor:default;width:100%}
.confidence-line:hover .confidence-popup,.confidence-line.popup-open .confidence-popup{display:block}
.confidence-info-icon{width:15px;height:15px;border-radius:50%;background:var(--surface);border:1px solid var(--sep);display:inline-flex;align-items:center;justify-content:center;font-size:9px;cursor:pointer;color:var(--text3);font-style:normal;line-height:1;flex-shrink:0;transition:background .15s,border-color .15s;user-select:none}
.confidence-info-icon:hover{background:var(--hover);border-color:var(--text3)}
.confidence-popup{display:none;position:absolute;top:calc(100% + 8px);left:50%;transform:translateX(-50%);background:var(--surface);border:1px solid var(--sep);border-radius:10px;padding:14px 16px;font-size:13px;font-weight:400;color:var(--text2);letter-spacing:0;text-transform:none;line-height:1.55;width:300px;z-index:200;box-shadow:0 4px 24px rgba(0,0,0,.2);pointer-events:none}
.confidence-popup.has-link{pointer-events:auto}
.confidence-popup.has-link a{font-size:12px;font-weight:600;margin-top:8px;display:inline-block}
.stat-label-wrap{position:relative;display:inline-flex;align-items:center;justify-content:center;gap:4px;margin-top:8px}
.stat-label-wrap:hover .confidence-popup,.stat-label-wrap.popup-open .confidence-popup{display:block}
.stat-link{color:inherit;text-decoration:none}
.stat-link:hover{color:inherit;text-decoration:none}
.bar-track{flex:1;height:4px;background:var(--sep);border-radius:2px;overflow:hidden}
.bar-fill{height:100%;background:var(--accent);border-radius:2px;transition:width .6s ease}
.bar-label{font-size:13px;font-weight:500;color:var(--text2);font-family:'SF Mono',ui-monospace,monospace;min-width:40px;text-align:right;letter-spacing:-.02em}

.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:24px;padding:48px 0;border-top:1px solid var(--sep);border-bottom:1px solid var(--sep);margin:48px 0}
.stat{display:flex;flex-direction:column;align-items:center;text-align:center;min-width:0;border-radius:8px;padding:12px 8px;transition:background .15s}
.stat.stat-has-link:hover{background:var(--hover);cursor:pointer}
.stat-value{font-family:'SF Mono',ui-monospace,monospace;font-size:22px;font-weight:600;color:var(--text);letter-spacing:-.03em;line-height:1}
.stat-label{font-size:11px;font-weight:500;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-top:8px}

.cta-row{display:flex;justify-content:center;gap:12px;padding-bottom:64px}
.btn{display:inline-flex;align-items:center;gap:6px;padding:10px 20px;border-radius:980px;font-size:14px;font-weight:500;
  cursor:pointer;transition:all .2s;border:none;text-decoration:none;font-family:inherit}
.btn:hover{text-decoration:none}
.btn-primary{background:var(--synthesize);color:var(--badge-fg)}
.btn-primary:hover{background:color-mix(in srgb,var(--synthesize) 80%,black);color:var(--badge-fg)}
.btn-secondary{background:var(--surface);color:var(--text);border:1px solid var(--sep)}
.btn-secondary:hover{background:var(--hover)}

.section{padding:64px 0}
.section+.section{border-top:1px solid var(--sep)}
.section-title{font-size:13px;font-weight:600;color:var(--text);text-transform:uppercase;letter-spacing:.04em;margin-bottom:32px}
.section-intro{font-size:17px;color:var(--text2);line-height:1.65;margin-bottom:32px;max-width:560px;text-align:center}
#about .section-intro{text-align:left}

.phase-grid{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--sep);border-radius:12px;overflow:hidden}
.phase-item{background:var(--bg);padding:24px;transition:background .15s;display:block;text-decoration:none;color:inherit}
.phase-item:hover{background:var(--hover);text-decoration:none;color:inherit}
.phase-dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:8px;vertical-align:middle;position:relative;}
.phase-name{font-size:14px;font-weight:600;color:var(--text);margin-bottom:8px}
.phase-desc{font-size:14px;color:var(--text2);line-height:1.5}

.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;
  text-transform:uppercase;letter-spacing:.04em;color:var(--badge-fg);background:var(--text3)}
.badge[data-phase="EXPLORE"]{background:var(--explore)}
.badge[data-phase="SYNTHESIZE"]{background:var(--synthesize)}
.badge[data-phase="CRITIQUE"]{background:var(--critique)}
.badge[data-phase="EVOLVE"]{background:var(--evolve)}

table{width:100%;border-collapse:collapse;font-size:15px;overflow:hidden}
th{text-align:left;padding:8px 12px;border-bottom:2px solid var(--sep);background:var(--surface);
  color:var(--text2);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.06em}
td{padding:8px 12px;border-bottom:1px solid var(--sep)}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--hover)}

code{font-family:'SF Mono',ui-monospace,monospace;font-size:.88em;background:var(--code-bg);
  padding:2px 6px;border-radius:4px;color:var(--accent)}
.how-to-run code{color:var(--text)}
pre{background:var(--surface);border:1px solid var(--sep);border-radius:12px;padding:16px 20px;overflow-x:auto;margin:16px 0}
pre code{background:none;padding:0;color:var(--text);font-size:14px}

.journal-day{margin-bottom:32px}
.journal-day summary{list-style:none;cursor:pointer;font-size:13px;font-weight:600;color:var(--text3);
  text-transform:uppercase;letter-spacing:.04em;padding:12px 0;border-bottom:1px solid var(--sep);
  display:flex;align-items:center;gap:6px;user-select:none;transition:color .15s}
.journal-day summary:hover{color:var(--text)}
.journal-day summary::-webkit-details-marker{display:none}
.journal-day summary::before{content:'';display:inline-block;width:0;height:0;
  border-left:5px solid var(--text3);border-top:4px solid transparent;border-bottom:4px solid transparent;transition:transform .2s}
.journal-day[open] summary::before{transform:rotate(90deg)}
.journal-day .day-entries{display:flex;flex-direction:column;gap:0}
.journal-card{display:flex;gap:16px;align-items:flex-start;padding:16px 12px;margin:0 -12px;
  border-radius:8px;transition:background .12s;cursor:pointer}
.journal-card:hover{background:var(--hover)}
.journal-card a{color:inherit;display:flex;flex:1;min-width:0;text-decoration:none;gap:16px;align-items:flex-start}
.journal-card a:hover{text-decoration:none}
.journal-card-thumb{width:64px;height:64px;border-radius:8px;object-fit:cover;flex-shrink:0;background:var(--surface)}
.card-meta{display:flex;align-items:center;gap:8px;margin-bottom:4px;flex-wrap:wrap}
.card-date{font-size:13px;color:var(--text2);margin-left:auto;font-variant-numeric:tabular-nums}
.card-model{font-size:13px;color:var(--text2)}
.card-title{font-size:15px;font-weight:600;color:var(--text);margin-bottom:4px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.card-excerpt{font-size:15px;color:var(--text2);line-height:1.45;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}

.filter-bar{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:24px}
.filter-btn{appearance:none;border:1px solid var(--sep);background:transparent;color:var(--text2);
  border-radius:980px;padding:5px 14px;font-size:13px;font-weight:500;cursor:pointer;transition:all .15s;font-family:inherit}
.filter-btn:hover{color:var(--c,var(--text));border-color:var(--c,var(--text3))}
.filter-btn.active{background:var(--c,var(--text));color:var(--badge-fg,#000);border-color:var(--c,var(--text))}

.gallery-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:24px;padding:24px 0}
.gallery-card{background:var(--surface);border:1px solid var(--sep);border-radius:12px;overflow:hidden;transition:border-color .15s,box-shadow .15s}
.gallery-card:hover{border-color:var(--text3);box-shadow:0 8px 24px rgba(0,0,0,.08)}
.gallery-card-img-wrap{aspect-ratio:1;background:var(--bg);overflow:hidden}
.gallery-card-img-wrap a,.gallery-card-img-wrap .gallery-card-img-btn{display:block;height:100%;width:100%;cursor:pointer;border:none;background:none;padding:0}
.gallery-card img{width:100%;height:100%;object-fit:cover;display:block;vertical-align:top;pointer-events:none}
.gallery-card-meta{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:12px 14px;border-top:1px solid var(--sep)}
.gallery-card-title{font-size:15px;font-weight:600;color:var(--text);padding:0 14px 8px;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.gallery-card-prompt{font-size:14px;color:var(--text2);line-height:1.5;padding:0 14px 14px;max-height:4.5em;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical}
.gallery-card-prompt.expanded{max-height:none;-webkit-line-clamp:unset}
.gallery-card-prompt-toggle{font-size:12px;color:var(--text2);cursor:pointer;margin-top:4px;padding:0 14px 14px;display:block;background:none;border:none;text-align:left;font-family:inherit}
.gallery-card-prompt-toggle:hover{color:var(--text);text-decoration:underline}
.gallery-empty{text-align:center;padding:64px 24px;color:var(--text2)}
.insight-item{background:var(--surface);border:1px solid var(--sep);border-radius:12px;padding:20px;margin-bottom:16px}
.insight-item h3{font-size:15px;font-weight:600;color:var(--text);margin:0 0 8px;line-height:1.4}
.insight-meta{font-size:13px;color:var(--text2);margin-bottom:8px}
.insight-journal-details{margin-top:8px;margin-bottom:0;padding:0;border:none}
.insight-journal-details summary{padding:8px 0;border-bottom:none}
.insight-journal-details .day-entries{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;padding-top:8px}
.insight-journal-details .nav-arrow{max-width:100%}
.insight-journal-details .nav-arrow-title{max-width:320px;font-size:15px}
.gallery-empty p:first-child{font-size:18px;font-weight:500;color:var(--text);margin-bottom:8px}

.back-link{font-size:14px;color:var(--text2);margin-bottom:32px;display:inline-block;transition:color .15s}
.back-link:hover{color:var(--text);text-decoration:none}
.entry-header{margin-bottom:40px;padding-bottom:24px;border-bottom:1px solid var(--sep)}
.entry-meta-row{font-size:13px;color:var(--text2);margin-bottom:8px;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.entry-meta-row .badge{font-size:11px;padding:2px 8px}
.entry-title{font-size:32px;font-weight:700;letter-spacing:-.025em;line-height:1.15;margin-bottom:0}
.entry-meta{font-size:14px;color:var(--text2)}

.markdown-body{line-height:1.75;font-size:17px}
.markdown-body h1{font-size:28px;font-weight:700;margin:40px 0 12px;color:var(--text);letter-spacing:-.02em}
.markdown-body h2{font-size:22px;font-weight:600;margin:40px 0 12px;color:var(--text);letter-spacing:-.02em}
.markdown-body h3{font-size:17px;font-weight:600;margin:32px 0 8px;color:var(--text)}
.markdown-body p{margin:16px 0;color:var(--text2)}
.markdown-body ul,.markdown-body ol{margin:16px 0;padding-left:1.7rem}
.markdown-body li{margin:4px 0;color:var(--text2)}
.markdown-body blockquote{margin:24px 0;padding:16px 20px;background:var(--tint);border-radius:8px;
  color:var(--text2);font-style:italic;border:none}
.markdown-body img{max-width:100%;border-radius:12px;margin:24px 0;display:block}
.markdown-body hr{border:none;border-top:1px solid var(--sep);margin:32px 0}
.markdown-body hr:has(+ img),.markdown-body hr:has(+ details){border:none;margin:0;height:0;padding:0;overflow:hidden}
.markdown-body strong{color:var(--text);font-weight:600}
.markdown-body em{color:inherit}
.markdown-body code{font-family:'SF Mono',ui-monospace,monospace;font-size:.88em;background:var(--code-bg);
  padding:2px 6px;border-radius:4px;color:var(--accent)}
.markdown-body pre{background:var(--surface);border:1px solid var(--sep);border-radius:12px;padding:16px 20px;overflow-x:auto;margin:24px 0}
.markdown-body pre code{background:none;padding:0;font-size:14px;color:var(--text)}

.nav-pair{display:flex;flex-direction:column;gap:16px;margin-top:64px;padding-top:8px;padding-bottom:48px;border-top:1px solid var(--sep)}
.nav-pair-row{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
.nav-arrow{display:inline-flex;flex-direction:column;gap:7px;text-decoration:none;max-width:min(300px,44vw);transition:color .15s}
.nav-arrow:hover{text-decoration:none}
.nav-arrow--next{align-items:flex-end}
.nav-arrow--empty{color:var(--text3);cursor:default}
.nav-arrow-label{font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:.05em;color:var(--text3);transition:color .15s}
.nav-arrow:hover .nav-arrow-label{color:var(--text)}
.nav-arrow-top{display:inline-flex;align-items:center;gap:8px;min-height:22px}
.nav-split-badge{display:inline-flex;align-items:center;border-radius:4px;overflow:hidden;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;line-height:1;height:22px}
.nav-split-badge .split-cycle{background:#000;color:#fff;padding:0 6px;flex-shrink:0;display:inline-flex;align-items:center;height:100%}
.nav-split-badge .split-phase{color:var(--badge-fg);padding:0 7px;display:inline-flex;align-items:center;height:100%;background:var(--text3)}
.nav-split-badge .split-phase[data-phase="EXPLORE"]{background:var(--explore)}
.nav-split-badge .split-phase[data-phase="SYNTHESIZE"]{background:var(--synthesize)}
.nav-split-badge .split-phase[data-phase="CRITIQUE"]{background:var(--critique)}
.nav-split-badge .split-phase[data-phase="EVOLVE"]{background:var(--evolve)}
.nav-arrow-title{font-size:13px;color:var(--text2);line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;max-width:220px;transition:color .15s}
.nav-arrow:hover .nav-arrow-title{color:var(--text)}
.nav-arrow--next .nav-arrow-title{text-align:right}
.nav-all{font-size:13px;color:var(--text2);transition:color .15s}
.nav-all:hover{color:var(--text);text-decoration:none}

.empty-state{text-align:center;padding:64px 0;color:var(--text2)}

.site-footer{border-top:1px solid var(--sep);margin-top:96px}
.site-footer-inner{max-width:720px;margin:0 auto;padding:24px 24px 32px}
.site-footer-bottom{display:flex;justify-content:space-between;align-items:center;font-size:12px;color:var(--text3)}
.site-footer-bottom a{color:var(--text3);transition:color .15s}
.site-footer-bottom a:hover{color:var(--text2);text-decoration:none}

.latest-visual{max-width:560px;margin:0 auto 40px;text-align:center}
.latest-visual-card{border-radius:16px;overflow:hidden;background:var(--surface);border:1px solid var(--sep);transition:border-color .15s,box-shadow .15s}
.latest-visual-card:hover{border-color:var(--text3);box-shadow:0 4px 24px rgba(0,0,0,.18)}
.latest-visual-link{display:block;text-decoration:none;color:inherit;position:relative}
.latest-visual-link:hover{text-decoration:none;color:inherit}
.latest-visual-img{width:100%;display:block;max-height:360px;object-fit:cover}
.latest-visual-overlay{position:absolute;bottom:12px;left:12px;display:flex;align-items:center;pointer-events:none}
.latest-visual-overlay .nav-split-badge{box-shadow:0 2px 8px rgba(0,0,0,.3)}
.latest-visual-actions{margin-top:16px;display:flex;justify-content:center}
.latest-visual-caption{display:block;padding:12px 16px;font-size:13px;color:var(--text2);text-align:left;text-decoration:none}
.latest-visual-caption:hover{color:var(--text2);text-decoration:none}
.latest-visual-title{font-family:'Source Serif 4',Georgia,serif;font-size:17px;font-style:italic;line-height:1.65}
.findings-slider{padding:16px 0;}
.findings-label{font-size:13px;font-weight:600;color:var(--text2);text-transform:uppercase;letter-spacing:.04em;margin-bottom:20px;text-align:center}
.slider-viewport{overflow:hidden;position:relative}
.slider-track{display:flex;gap:16px;will-change:transform}
.slider-card{flex:0 0 100%;min-width:0;background:var(--surface);border:1px solid var(--sep);border-radius:14px;padding:28px 24px 20px;
  text-decoration:none;color:inherit;display:flex;gap:16px;align-items:flex-start;transition:background .15s,border-color .15s;-webkit-user-select:none;user-select:none}
.slider-card-body{flex:1;min-width:0;height:100%;display:flex;flex-direction:column}
.slider-card-thumb{width:64px;height:64px;border-radius:8px;object-fit:cover;flex-shrink:0;background:var(--surface)}
.slider-card:hover{background:var(--hover);border-color:var(--text3);text-decoration:none;color:inherit}
.slider-quote{font-family:'Source Serif 4',Georgia,serif;font-size:17px;font-style:italic;line-height:1.65;color:var(--text);
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;margin-bottom:16px}
.slider-time{font-size:12px;color:var(--text3);margin-bottom:12px;font-variant-numeric:tabular-nums}
.slider-meta{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--text3);flex-wrap:wrap;margin-top:auto}
.slider-read-more{font-size:13px;font-weight:400;color:var(--text2);font-style:normal;font-family:Outfit;margin-left:.35em;white-space:nowrap}
.slider-card:hover .slider-read-more{text-decoration:underline}
.slider-conf{display:inline-flex;align-items:center;gap:5px;margin-left:auto}
.slider-conf-track{width:48px;height:3px;background:var(--sep);border-radius:2px;overflow:hidden}
.slider-conf-fill{height:100%;background:var(--accent);border-radius:2px}
.slider-nav{display:flex;align-items:center;justify-content:center;gap:12px;margin-top:20px}
.slider-btn{appearance:none;border:1px solid var(--sep);background:transparent;color:var(--text3);width:36px;height:36px;border-radius:50%;
  cursor:pointer;display:inline-flex;align-items:center;justify-content:center;transition:all .15s;padding:0}
.slider-btn .icon-arrow{flex-shrink:0}
.slider-btn:hover:not(:disabled){color:var(--text);border-color:var(--text3)}
.slider-btn:disabled{opacity:.25;cursor:default}
.slider-dots{display:flex;gap:5px;align-items:center}
.slider-dot{width:5px;height:5px;border-radius:50%;background:var(--text3);opacity:.25;transition:opacity .2s,transform .2s}
.slider-dot.active{opacity:1;background:var(--synthesize);transform:scale(1.3)}

@media(max-width:600px){
  .hero{padding:64px 0 40px}
  .hero h1{font-size:34px}
  .thesis-block{font-size:20px}
  .stats{grid-template-columns:repeat(2,1fr)}
  .stat-value{font-size:20px}
  .insight-journal-details .day-entries{grid-template-columns:1fr}
  .phase-grid{grid-template-columns:1fr}
  .phase-strip .phase-arrow{display:none}
  .nav{position:relative}
  .nav-links{margin-left:0}
  .nav-right{margin-left:auto}
  .nav-hamburger{display:flex}
  .nav-links{position:absolute;top:100%;right:0;left:0;flex-direction:column;align-items:stretch;
    gap:0;padding:0 24px;background:rgba(26,26,28,.15);backdrop-filter:saturate(180%) blur(20px);
    -webkit-backdrop-filter:saturate(180%) blur(5px);border-bottom:1px solid var(--sep);
    box-shadow:0 4px 8px rgba(0,0,0,.1);max-height:0;overflow:hidden;opacity:0;
    transition:max-height .25s ease,opacity .2s ease}
  [data-theme="light"] .nav-links{background:rgba(255,255,255,.15)}
  .nav.nav-open .nav-links{max-height:240px;opacity:1}
  .nav-links a{display:flex;justify-content:space-between;align-items:center;padding:12px 0;font-size:15px;border-bottom:1px solid var(--sep)}
  .nav-links a:last-of-type{border-bottom:none}
  .nav-links a::after{content:'›';font-size:20px;font-weight:500;color:var(--text3);flex-shrink:0;margin-left:12px;transition:color .15s}
  .nav-links a:hover::after,.nav-links a.active::after{color:var(--text)}
  .card-date{margin-left:0;flex-basis:100%}
  .entry-title{font-size:26px}
  .slider-quote{font-size:15px}
  .findings-slider{padding:32px 0}
}

.gallery-modal{position:fixed;inset:0;z-index:1000;display:flex;align-items:center;justify-content:center;
  background:rgba(0,0,0,.92);backdrop-filter:blur(8px);opacity:0;visibility:hidden;transition:opacity .2s,visibility .2s;
  touch-action:pan-y}
.gallery-modal.open{opacity:1;visibility:visible}
.gallery-modal-close{appearance:none;border:none;background:rgba(255,255,255,.1);color:var(--text);width:44px;height:44px;
  border-radius:50%;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:24px;line-height:1;
  transition:background .15s,color .15s;flex-shrink:0}
.gallery-modal-close:hover{background:rgba(255,255,255,.2)}
.gallery-modal-top{position:absolute;top:16px;right:16px;z-index:1002;display:flex;align-items:center;gap:12px}
.gallery-modal-counter{font-size:14px;color:var(--text2)}
.gallery-modal-inner{display:flex;align-items:center;gap:16px;max-width:100%;padding:60px 16px 24px}
.gallery-modal-prev,.gallery-modal-next{flex-shrink:0;appearance:none;border:none;background:rgba(255,255,255,.1);color:var(--text);
  width:48px;height:48px;border-radius:50%;cursor:pointer;display:flex;align-items:center;justify-content:center;
  transition:background .15s,color .15s}
.gallery-modal-prev:hover,.gallery-modal-next:hover{background:rgba(255,255,255,.2)}
.gallery-modal-center{display:flex;flex-direction:column;align-items:center;gap:12px;flex:1;min-width:0}
.gallery-modal-img-wrap{display:flex;align-items:center;justify-content:center;cursor:pointer;max-height:75vh}
.gallery-modal-img-wrap img{max-width:100%;max-height:75vh;width:auto;height:auto;object-contain;border-radius:8px;display:block}
.gallery-modal-title{font-size:15px;color:var(--text2);text-align:center;line-height:1.4;max-width:560px}
.gallery-modal-nav-row{display:none;justify-content:center;gap:12px;margin-top:12px}
@media(max-width:600px){
  .gallery-modal-inner{flex-direction:column;padding:60px 12px 24px}
  .gallery-modal-prev,.gallery-modal-next{position:static;transform:none}
  .gallery-modal-nav-row{display:flex}
  .gallery-modal-side-nav{display:none}
}
.gallery-modal-side-nav{display:flex}
`;

const ARROW_LEFT = '<svg class="icon-arrow" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg>';
const ARROW_RIGHT = '<svg class="icon-arrow" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18l6-6-6-6"/></svg>';
const ARROW_RIGHT_SM = '<svg class="icon-arrow" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18l6-6-6-6"/></svg>';

// ── Seeker background animation ───────────────────────────

const SEEKER_SCRIPT = `(function(){
var canvas=document.getElementById('seeker-bg');if(!canvas)return;
var ctx=canvas.getContext('2d');
var W,H,pageH,dpr;

var isDark=document.documentElement.getAttribute('data-theme')!=='light';
new MutationObserver(function(){isDark=document.documentElement.getAttribute('data-theme')!=='light';}).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});

function themeColor(hue,act){
  if(isDark)return[Math.round(48+hue*15+act*30),Math.round(209-hue*20+act*20),Math.round(88+hue*25+act*15)];
  return[Math.round(45+hue*10+act*15),Math.round(85+hue*15+act*20),Math.round(140+hue*20+act*25)];
}
function trailColor(){return isDark?[48,209,88]:[50,95,150];}
function headColor(){return isDark?[60,220,100]:[55,110,165];}

var GRAD=[[1,1],[-1,1],[1,-1],[-1,-1],[1,0],[-1,0],[0,1],[0,-1]];
var perm=new Uint8Array(512);
(function(){var p=new Uint8Array(256);for(var i=0;i<256;i++)p[i]=i;
for(var i=255;i>0;i--){var j=(Math.random()*(i+1))|0;var t=p[i];p[i]=p[j];p[j]=t;}
for(var i=0;i<512;i++)perm[i]=p[i&255];})();

function noise2D(x,y){
  var F2=0.5*(Math.sqrt(3)-1),G2=(3-Math.sqrt(3))/6;
  var s=(x+y)*F2,i=Math.floor(x+s),j=Math.floor(y+s),t=(i+j)*G2;
  var x0=x-(i-t),y0=y-(j-t),i1=x0>y0?1:0,j1=x0>y0?0:1;
  var x1=x0-i1+G2,y1=y0-j1+G2,x2=x0-1+2*G2,y2=y0-1+2*G2;
  var ii=i&255,jj=j&255;
  function dot(gi,xp,yp){var g=GRAD[gi%8];return g[0]*xp+g[1]*yp;}
  var n0=0,n1=0,n2=0;
  var t0=0.5-x0*x0-y0*y0;if(t0>0){t0*=t0;n0=t0*t0*dot(perm[ii+perm[jj]],x0,y0);}
  var t1=0.5-x1*x1-y1*y1;if(t1>0){t1*=t1;n1=t1*t1*dot(perm[ii+i1+perm[jj+j1]],x1,y1);}
  var t2=0.5-x2*x2-y2*y2;if(t2>0){t2*=t2;n2=t2*t2*dot(perm[ii+1+perm[jj+1]],x2,y2);}
  return 70*(n0+n1+n2);
}

function smoothstep(a,b,t){var x=Math.max(0,Math.min(1,(t-a)/(b-a)));return x*x*(3-2*x);}

var SPACING=20,grid=[],gridCols,gridRows;
function initGrid(){
  pageH=Math.max(document.documentElement.scrollHeight,H);
  gridCols=Math.ceil(W/SPACING)+2;gridRows=Math.ceil(pageH/SPACING)+2;
  var ox=(W-(gridCols-1)*SPACING)/2;grid=[];
  for(var r=0;r<gridRows;r++)for(var c=0;c<gridCols;c++){
    var jx=noise2D(c*0.7,r*0.7)*SPACING*0.2,jy=noise2D(c*0.7+100,r*0.7+100)*SPACING*0.2;
    grid.push({x:ox+c*SPACING+jx,y:r*SPACING+jy,activation:0,hue:noise2D(c*0.3,r*0.3+50)});
  }
}
function resize(){
  dpr=window.devicePixelRatio||1;W=window.innerWidth;H=window.innerHeight;
  canvas.width=W*dpr;canvas.height=H*dpr;ctx.setTransform(dpr,0,0,dpr,0,0);initGrid();
}

var seeker=null;
function createSeeker(){return{x:W*0.2+Math.random()*W*0.6,y:H*0.3,heading:Math.random()*Math.PI*2,speed:1.8,turnRate:0.45,noiseOffset:Math.random()*1000,glowRadius:80,trail:[]};}

var currentT=0,mouse={x:-9999,y:-9999,active:false,lastMoveTime:0};
var sY=0,lastSY=0,smoothSV=0;
var ringPulses=[],shockwaves=[],seekerFoundMouse=false;
var FOUND_RADIUS=25,FOUND_COOLDOWN=3,lastFoundTime=-999,seekBoost=0;
var orbiting=false,orbitCenter={x:0,y:0},orbitAngle=0,orbitSwept=0;
var ORBIT_RADIUS=40,ORBIT_SPEED=1.8,orbitArcTarget=0;

document.addEventListener('mousemove',function(e){mouse.x=e.clientX;mouse.y=e.clientY+sY;mouse.active=true;mouse.lastMoveTime=currentT;seekerFoundMouse=false;orbiting=false;});
document.addEventListener('mouseleave',function(){mouse.active=false;seekerFoundMouse=false;});
window.addEventListener('scroll',function(){sY=window.scrollY;if(mouse.active)mouse.y+=(sY-lastSY);},{passive:true});

resize();seeker=createSeeker();window.addEventListener('resize',resize);

function breathe(t){var ph=((t*0.14)%1+1)%1;return smoothstep(0,0.3,ph)*(1-smoothstep(0.3,1,ph))*1.7;}
function heartbeat(t){var ph=((t*0.85)%1+1)%1;return Math.exp(-Math.pow((ph-0.12)*9,2))+Math.exp(-Math.pow((ph-0.28)*11,2))*0.5;}
function edgeFactor(docX){var cx=W/2,dx=Math.abs(docX-cx);return 0.15+smoothstep(0,cx*0.7,dx)*0.85;}

var ACTIVATION_DECAY=0.985,TRAIL_LEN=250;
var hid=false;document.addEventListener('visibilitychange',function(){hid=document.hidden;});

function draw(ts){
  requestAnimationFrame(draw);if(hid)return;
  var t=ts*0.001;currentT=t;ctx.clearRect(0,0,W,H);
  var rawVel=sY-lastSY;smoothSV+=(rawVel-smoothSV)*(Math.abs(rawVel)>Math.abs(smoothSV)?0.3:0.04);lastSY=sY;
  var br=breathe(t),hb=heartbeat(t);
  var rhythmSpeed=0.7+br*0.4+hb*0.3,rhythmGlow=0.6+br*0.3+hb*0.2;
  var s=seeker,viewTop=sY-100,viewBot=sY+H+100;

  var steerNoise=noise2D(s.x*0.003+s.noiseOffset+t*0.08,s.y*0.003+t*0.06);
  var courseChange=noise2D(s.x*0.001+s.noiseOffset+500,t*0.2);
  var sharpTurn=Math.abs(courseChange)>0.6?courseChange*1.5:0;
  s.heading+=(steerNoise*s.turnRate+sharpTurn*0.3)/60*3;

  var viewCenterY=sY+H/2,dyToView=viewCenterY-s.y,dxToCenter=W/2-s.x;
  var distToView=Math.sqrt(dxToCenter*dxToCenter+dyToView*dyToView);
  if(distToView>H*0.6){var toView=Math.atan2(dyToView,dxToCenter);var diff=toView-s.heading;while(diff>Math.PI)diff-=Math.PI*2;while(diff<-Math.PI)diff+=Math.PI*2;s.heading+=diff*0.015;}

  var seeking=false;
  if(orbiting){
    var step=ORBIT_SPEED/ORBIT_RADIUS;orbitAngle+=step;orbitSwept+=step;
    s.x=orbitCenter.x+Math.cos(orbitAngle)*ORBIT_RADIUS;s.y=orbitCenter.y+Math.sin(orbitAngle)*ORBIT_RADIUS;
    s.heading=orbitAngle+Math.PI*0.5;
    if(orbitSwept>=orbitArcTarget){orbiting=false;seekerFoundMouse=true;}
  }else if(mouse.active&&!seekerFoundMouse){
    var idleSec=t-mouse.lastMoveTime,idleFactor=smoothstep(0,2.5,idleSec);
    var attractRange=400+idleFactor*1200,baseStrength=0.02+idleFactor*0.12;
    var mdx=mouse.x-s.x,mdy=mouse.y-s.y,md=Math.sqrt(mdx*mdx+mdy*mdy);
    if(md>5){var proximity=Math.max(0,1-md/attractRange);var toMouse=Math.atan2(mdy,mdx);var diff=toMouse-s.heading;while(diff>Math.PI)diff-=Math.PI*2;while(diff<-Math.PI)diff+=Math.PI*2;s.heading+=diff*baseStrength*proximity;}
    seeking=idleFactor>0.1&&md<attractRange;
    if(md<FOUND_RADIUS&&(t-lastFoundTime)>FOUND_COOLDOWN){lastFoundTime=t;ringPulses.push({x:mouse.x,y:mouse.y,birth:t});shockwaves.push({x:mouse.x,y:mouse.y,birth:t});orbiting=true;orbitCenter.x=mouse.x;orbitCenter.y=mouse.y;orbitAngle=Math.atan2(s.y-mouse.y,s.x-mouse.x);orbitSwept=0;orbitArcTarget=(Math.PI/6)+Math.random()*(Math.PI*4/3);}
  }

  var seekTarget=(seeking||orbiting)?1.8:0;seekBoost+=(seekTarget-seekBoost)*((seeking||orbiting)?0.03:0.008);
  if(!orbiting){
    s.heading+=smoothSV*0.002;
    var approachBrake=1;
    if(seeking&&mouse.active){var mdx2=mouse.x-s.x,mdy2=mouse.y-s.y,md2=Math.sqrt(mdx2*mdx2+mdy2*mdy2);if(md2<150)approachBrake=0.3+0.7*(md2/150);}
    var speed=s.speed*rhythmSpeed*(1+seekBoost)*approachBrake;s.x+=Math.cos(s.heading)*speed;s.y+=Math.sin(s.heading)*speed;
  }

  if(s.x<20){s.x=20;s.heading=Math.PI-s.heading;}if(s.x>W-20){s.x=W-20;s.heading=Math.PI-s.heading;}
  if(s.y<20){s.y=20;s.heading=-s.heading;}if(s.y>pageH-20){s.y=pageH-20;s.heading=-s.heading;}
  s.trail.push({x:s.x,y:s.y,t:t});if(s.trail.length>TRAIL_LEN)s.trail.shift();

  var glowR=s.glowRadius*rhythmGlow,glowR2=glowR*glowR;
  for(var i=0;i<grid.length;i++){var g=grid[i];if(g.y<s.y-glowR||g.y>s.y+glowR)continue;var dx=g.x-s.x,dy=g.y-s.y,d2=dx*dx+dy*dy;if(d2<glowR2){var dist=Math.sqrt(d2),influence=1-dist/glowR,activation=influence*influence;if(activation>g.activation)g.activation=activation;}}

  for(var wi=shockwaves.length-1;wi>=0;wi--){var sw=shockwaves[wi];var age=t-sw.birth;if(age>6){shockwaves.splice(wi,1);continue;}var waveRadius=age*250,waveWidth=120+age*40,waveFade=Math.max(0,1-age/6),intensity=waveFade*waveFade*0.9;for(var i=0;i<grid.length;i++){var g=grid[i];var dx=g.x-sw.x,dy=g.y-sw.y,dist=Math.sqrt(dx*dx+dy*dy),fromRing=Math.abs(dist-waveRadius);if(fromRing<waveWidth){var ringProx=1-fromRing/waveWidth,act=ringProx*ringProx*intensity;if(act>g.activation)g.activation=act;}}}

  for(var i=0;i<grid.length;i++)grid[i].activation*=ACTIVATION_DECAY;

  var menuOpen=document.body.getAttribute('data-menu-open')==='true';
  var baseMult=menuOpen?3.5:1,actMult=menuOpen?2.2:1;
  for(var i=0;i<grid.length;i++){var g=grid[i];if(g.y<viewTop||g.y>viewBot)continue;var ef=edgeFactor(g.x),act=g.activation;var baseAlpha=0.02*ef*baseMult,actAlpha=act*0.3*ef*actMult,alpha=baseAlpha+actAlpha;if(alpha<0.003)continue;var baseSize=0.8+ef*0.5,actSize=act*2.2,size=baseSize+actSize,screenY=g.y-sY;var c=themeColor(g.hue,act);ctx.beginPath();ctx.arc(g.x,screenY,Math.max(0.3,size),0,Math.PI*2);ctx.fillStyle='rgba('+c[0]+','+c[1]+','+c[2]+','+alpha+')';ctx.fill();if(act>0.15){var glowAlpha=(act-0.15)*0.12*ef*(menuOpen?1.5:1);ctx.beginPath();ctx.arc(g.x,screenY,size*3,0,Math.PI*2);ctx.fillStyle='rgba('+c[0]+','+c[1]+','+c[2]+','+glowAlpha+')';ctx.fill();}}

  var trail=s.trail;var tc=trailColor();
  for(var i=0;i<trail.length;i++){var tp=trail[i];if(tp.y<viewTop||tp.y>viewBot)continue;var ef=edgeFactor(tp.x),progress=i/trail.length,fade=progress*progress;var age=t-tp.t,ageFade=Math.max(0,1-age*0.3),alpha=fade*ageFade*0.16*ef;if(alpha<0.002)continue;var size=0.8+fade*1.4,screenY=tp.y-sY;ctx.beginPath();ctx.arc(tp.x,screenY,size,0,Math.PI*2);ctx.fillStyle='rgba('+tc[0]+','+tc[1]+','+tc[2]+','+alpha+')';ctx.fill();}

  var headScreenY=s.y-sY;
  if(headScreenY>-50&&headScreenY<H+50){var hef=edgeFactor(s.x),headAlpha=0.4*hef*rhythmGlow,headSize=3+hb*2;var hc=headColor(),tc2=trailColor();ctx.beginPath();ctx.arc(s.x,headScreenY,headSize,0,Math.PI*2);ctx.fillStyle='rgba('+hc[0]+','+hc[1]+','+hc[2]+','+headAlpha+')';ctx.fill();var bloomSize=headSize*6;var grad=ctx.createRadialGradient(s.x,headScreenY,0,s.x,headScreenY,bloomSize);grad.addColorStop(0,'rgba('+hc[0]+','+hc[1]+','+hc[2]+','+(headAlpha*0.4)+')');grad.addColorStop(0.35,'rgba('+tc2[0]+','+tc2[1]+','+tc2[2]+','+(headAlpha*0.12)+')');grad.addColorStop(1,'rgba('+tc2[0]+','+tc2[1]+','+tc2[2]+',0)');ctx.beginPath();ctx.arc(s.x,headScreenY,bloomSize,0,Math.PI*2);ctx.fillStyle=grad;ctx.fill();}

  var rpc=headColor();
  for(var i=ringPulses.length-1;i>=0;i--){var rp=ringPulses[i];var age=t-rp.birth;if(age>2.5){ringPulses.splice(i,1);continue;}var rpScreenY=rp.y-sY;if(rpScreenY<-200||rpScreenY>H+200)continue;var expand=age*80,fade=Math.max(0,1-age/2.5),ringAlpha=fade*fade*0.35;ctx.beginPath();ctx.arc(rp.x,rpScreenY,expand,0,Math.PI*2);ctx.strokeStyle='rgba('+rpc[0]+','+rpc[1]+','+rpc[2]+','+ringAlpha+')';ctx.lineWidth=Math.max(0.5,2.5-age*0.8);ctx.stroke();}
}
requestAnimationFrame(draw);
})()`;

const _DEAD2 = `REMOVED
uniform float uScroll;
uniform float uScrollVel;
uniform float uTheme;

vec3 mod289(vec3 x){return x-floor(x*(1./289.))*289.;}
vec2 mod289(vec2 x){return x-floor(x*(1./289.))*289.;}
vec3 permute(vec3 x){return mod289(((x*34.)+1.)*x);}

float snoise(vec2 v){
  const vec4 C=vec4(.211324865405187,.366025403784439,-.577350269189626,.024390243902439);
  vec2 i=floor(v+dot(v,C.yy));vec2 x0=v-i+dot(i,C.xx);
  vec2 i1=(x0.x>x0.y)?vec2(1.,0.):vec2(0.,1.);
  vec4 x12=x0.xyxy+C.xxzz;x12.xy-=i1;i=mod289(i);
  vec3 p=permute(permute(i.y+vec3(0.,i1.y,1.))+i.x+vec3(0.,i1.x,1.));
  vec3 m=max(.5-vec3(dot(x0,x0),dot(x12.xy,x12.xy),dot(x12.zw,x12.zw)),0.);
  m=m*m;m=m*m;
  vec3 x=2.*fract(p*C.www)-1.;vec3 h=abs(x)-.5;
  vec3 ox=floor(x+.5);vec3 a0=x-ox;
  m*=1.79284291400159-.85373472095314*(a0*a0+h*h);
  vec3 g;g.x=a0.x*x0.x+h.x*x0.y;g.yz=a0.yz*x12.xz+h.yz*x12.yw;
  return 130.*dot(m,g);
}

float fbm(vec2 p){
  float s=0.,a=.5;
  for(int i=0;i<4;i++){s+=snoise(p)*a;p*=2.05;a*=.48;}
  return s;
}

float ridged(vec2 p){
  float s=0.,a=.55,prev=1.;
  for(int i=0;i<5;i++){float n=1.-abs(snoise(p));n=n*n;s+=n*a*prev;prev=n;p*=2.1;a*=.47;}
  return s;
}

float heartbeat(float t){
  float ph=fract(t);
  return exp(-pow((ph-.15)*7.,2.))+exp(-pow((ph-.32)*9.,2.))*.55;
}

float breathe(float t){
  float ph=fract(t);
  return smoothstep(0.,.35,ph)*(1.-smoothstep(.35,1.,ph))*1.8;
}

void main(){
  vec2 uv=gl_FragCoord.xy/uResolution;
  float aspect=uResolution.x/uResolution.y;
  vec2 p=vec2(uv.x*aspect,uv.y)*1.8;
  float t=uTime;
  float sv=uScrollVel;
  vec2 sw=vec2(snoise(p*1.5+t*.3)*sv*.006,snoise(p*1.5+t*.3+99.)*sv*.008);
  vec2 q=vec2(fbm(p+sw+t*.008),fbm(p+sw+vec2(5.2,1.3)+t*.006));
  vec2 r=vec2(fbm(p+3.5*q+vec2(1.7,9.2)+t*.005+sw*2.),fbm(p+3.5*q+vec2(8.3,2.8)+t*.004+sw*2.));
  vec2 w=p+r*1.2+sw*.5;
  float vs=ridged(w*.8);float fv=ridged(w*1.8+vec2(33.,77.));
  float vS=pow(vs*.65+fv*.35,1.3);
  float sp=uScroll*.0004;float lp=fbm(w*.25+50.);
  float rhy=breathe(t*.14+lp*1.8+sp)*.6+heartbeat(t*.35+lp*2.2+sp)*.4;
  rhy=.15+rhy*.85;
  float si=abs(sv);rhy+=si*.04*smoothstep(.3,0.,abs(snoise(w+t*.5)));
  float sf=smoothstep(0.,8.,si)*.3;
  float sn1=snoise(w*8.+t*.3);float sn2=snoise(w*12.-t*.2+vec2(100.));
  float spk=smoothstep(.48,.7,sn1)*smoothstep(.2,.5,sn2);
  float pn=snoise(w*22.+t*.5+vec2(55.,13.));float pin=smoothstep(.72,.82,pn);
  float en=snoise(w*5.-t*.15+vec2(200.));float emb=smoothstep(.65,.8,en)*smoothstep(.15,.4,vS);
  spk*=smoothstep(.1,.35,vS);pin*=smoothstep(.05,.25,vS);
  float tw1=sin(t*2.5+snoise(w*20.)*6.28)*.5+.5;
  float tw2=sin(t*3.7+snoise(w*30.+77.)*6.28)*.5+.5;
  spk*=rhy*.6+tw1*.4;pin*=tw2*.6+rhy*.4;emb*=rhy;
  spk+=sf*smoothstep(.35,.55,sn1);pin+=sf*smoothstep(.6,.75,pn)*.5;
  float aS=spk+pin*1.4+emb*.7;
  float mi=0.;
  if(uMouseActive>.5){
    vec2 mp=uMouse/uResolution;mp.y=1.-mp.y;
    vec2 mUv=vec2(mp.x*aspect,mp.y)*1.8;
    float md=length(w-mUv)+snoise(w*2.+t*.2)*.3;
    mi=pow(smoothstep(1.5,0.,md),2.);
  }
  vec3 bg=mix(vec3(.19,.82,.35),vec3(.14,.54,.24),uTheme);
  vec3 wg=mix(vec3(.35,.88,.28),vec3(.22,.62,.18),uTheme);
  vec3 ct=mix(vec3(.12,.65,.55),vec3(.08,.45,.38),uTheme);
  float cv=snoise(w*.4+42.)*.5+.5;
  vec3 vc=mix(ct,wg,cv);vc=mix(bg,vc,.25);
  float tl=vS*rhy*.05+aS*.14+mi*vS*.07+mi*aS*.18+smoothstep(-.5,.8,fbm(w*.3+t*.01))*.006*rhy;
  vec3 bs=mix(vec3(.4,.9,.25),vec3(.3,.7,.15),uTheme);
  vec3 oc=mix(vc,bs,smoothstep(.04,.12,tl)*.3);
  float aScale=mix(1.,2.5,uTheme);
  gl_FragColor=vec4(oc,clamp(tl*aScale,0.,.7));
}`;

const MYCELIUM_INIT = `(function(){
var c=document.getElementById('mycelium-bg');if(!c)return;
var gl=c.getContext('webgl',{alpha:true,premultipliedAlpha:false,antialias:false});
if(!gl)return;
var RS=window.innerWidth<768?.4:.55;
function resize(){
  c.width=Math.floor(window.innerWidth*RS);
  c.height=Math.floor(window.innerHeight*RS);
  gl.viewport(0,0,c.width,c.height);
}
resize();window.addEventListener('resize',resize);
function cs(src,type){var s=gl.createShader(type);gl.shaderSource(s,src);gl.compileShader(s);
  if(!gl.getShaderParameter(s,gl.COMPILE_STATUS)){console.error(gl.getShaderInfoLog(s));return null;}return s;}
var vs=cs(document.getElementById('ms-v').textContent,gl.VERTEX_SHADER);
var fs=cs(document.getElementById('ms-f').textContent,gl.FRAGMENT_SHADER);
if(!vs||!fs)return;
var pg=gl.createProgram();gl.attachShader(pg,vs);gl.attachShader(pg,fs);gl.linkProgram(pg);
if(!gl.getProgramParameter(pg,gl.LINK_STATUS)){console.error(gl.getProgramInfoLog(pg));return;}
gl.useProgram(pg);
var aP=gl.getAttribLocation(pg,'aPos');
var bf=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,bf);
gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,1,-1,-1,1,1,1]),gl.STATIC_DRAW);
gl.enableVertexAttribArray(aP);gl.vertexAttribPointer(aP,2,gl.FLOAT,false,0,0);
var uR=gl.getUniformLocation(pg,'uResolution'),uT=gl.getUniformLocation(pg,'uTime'),
    uM=gl.getUniformLocation(pg,'uMouse'),uMA=gl.getUniformLocation(pg,'uMouseActive'),
    uS=gl.getUniformLocation(pg,'uScroll'),uSV=gl.getUniformLocation(pg,'uScrollVel'),
    uTh=gl.getUniformLocation(pg,'uTheme');
var mx=0,my=0,mAct=0,sPos=0,lsPos=0,sSV=0,cTh=0,tTh=0;
function gTh(){return document.documentElement.getAttribute('data-theme')==='light'?1.:0.;}
tTh=gTh();cTh=tTh;
new MutationObserver(function(){tTh=gTh();}).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
document.addEventListener('mousemove',function(e){mx=e.clientX*RS;my=e.clientY*RS;mAct=1;});
document.addEventListener('mouseleave',function(){mAct=0;});
window.addEventListener('scroll',function(){sPos=window.scrollY;},{passive:true});
gl.clearColor(0,0,0,0);
var t0=0,hid=false;
document.addEventListener('visibilitychange',function(){hid=document.hidden;});
function draw(ts){
  requestAnimationFrame(draw);if(hid)return;
  if(!t0)t0=ts;var t=(ts-t0)*.001;
  cTh+=(tTh-cTh)*.08;
  var rv=sPos-lsPos;sSV+=(rv-sSV)*(Math.abs(rv)>Math.abs(sSV)?.3:.04);lsPos=sPos;
  gl.clear(gl.COLOR_BUFFER_BIT);
  gl.uniform2f(uR,c.width,c.height);gl.uniform1f(uT,t);
  gl.uniform2f(uM,mx,my);gl.uniform1f(uMA,mAct);
  gl.uniform1f(uS,sPos);gl.uniform1f(uSV,sSV);gl.uniform1f(uTh,cTh);
  gl.drawArrays(gl.TRIANGLE_STRIP,0,4);
}
requestAnimationFrame(draw);
})();`;

// ── HTML shell ────────────────────────────────────────────

function shell(title, body, activeNav = "", description = "") {
  const desc = description || SEO_DESCRIPTION;
  const fullTitle = title === "Home" ? "The Meaning Seeker - Why are we here?" : `${title} — Meaning Seeker`;
  return `<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>${fullTitle}</title>
<meta name="description" content="${desc}">
<meta name="theme-color" content="#30d158">
<meta name="author" content="10101">
<meta property="og:type" content="website">
<meta property="og:title" content="${fullTitle}">
<meta property="og:description" content="${desc}">
<meta property="og:site_name" content="${SEO_SITE_NAME}">
<meta property="og:image" content="${(process.env.SITE_URL || '').replace(/\/$/, '')}/assets/TheMeaningSeeker.jpg">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="${fullTitle}">
<meta name="twitter:description" content="${desc}">
<meta name="twitter:image" content="${(process.env.SITE_URL || '').replace(/\/$/, '')}/assets/TheMeaningSeeker.jpg">
<link rel="icon" type="image/png" href="/favicon-96x96.png" sizes="96x96">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="shortcut icon" href="/favicon.ico">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
<link rel="manifest" href="/site.webmanifest">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;1,8..60,400&display=swap" rel="stylesheet">
<style>${CSS}</style>
</head>
<body>
<canvas id="seeker-bg"></canvas>
<nav class="nav">
  <a href="/" class="nav-brand">Meaning Seeker</a>
  <div class="nav-links">
    <a href="/"${activeNav === "home" ? ' class="active"' : ""}>Home</a>
    <a href="/journal"${activeNav === "journal" ? ' class="active"' : ""}>Journal</a>
    <a href="/insights"${activeNav === "insights" ? ' class="active"' : ""}>Insights</a>
    <a href="/gallery"${activeNav === "gallery" ? ' class="active"' : ""}>Gallery</a>
    <a href="/how-to"${activeNav === "how-to" ? ' class="active"' : ""}>How to run</a>
  </div>
  <div class="nav-right">
    <a href="https://github.com/matiyin/meaning-seeker.com" target="_blank" rel="noopener noreferrer" class="nav-github" title="GitHub repository" aria-label="GitHub repository">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/></svg>
    </a>
    <button class="theme-toggle" id="theme-toggle" title="Toggle theme">
      <svg id="icon-moon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
      <svg id="icon-sun" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:none"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
    </button>
    <button class="nav-hamburger" id="nav-hamburger" aria-label="Toggle menu" aria-expanded="false">
      <span class="nav-menu-icon">?</span>
    </button>
  </div>
</nav>
<main class="page-content">${body}</main>
<script>
(function(){
  var h=document.documentElement,t=document.getElementById('theme-toggle'),
      m=document.getElementById('icon-moon'),s=document.getElementById('icon-sun');
  function sys(){return window.matchMedia('(prefers-color-scheme:light)').matches?'light':'dark'}
  function set(v){h.setAttribute('data-theme',v);m.style.display=v==='dark'?'':'none';s.style.display=v==='light'?'':'none'}
  var st=localStorage.getItem('ms-theme');set(st||sys());
  t.addEventListener('click',function(){var c=h.getAttribute('data-theme');var n=c==='dark'?'light':'dark';set(n);localStorage.setItem('ms-theme',n)});
  window.matchMedia('(prefers-color-scheme:light)').addEventListener('change',function(e){if(!localStorage.getItem('ms-theme'))set(e.matches?'light':'dark')});
})();(function(){
  var nav=document.querySelector('.nav');if(!nav)return;
  var lastY=window.scrollY||0;var threshold=60;
  function onScroll(){var y=window.scrollY||0;
    if(y<=threshold){nav.classList.remove('nav-hidden');lastY=y;return}
    if(y>lastY)nav.classList.add('nav-hidden');
    else nav.classList.remove('nav-hidden');
    lastY=y;
  }
  var ticking=false;window.addEventListener('scroll',function(){if(!ticking){requestAnimationFrame(function(){onScroll();ticking=false});ticking=true}},{passive:true});
})();(function(){
  var btn=document.getElementById('nav-hamburger');var nav=document.querySelector('.nav');
  if(!btn||!nav)return;
  function setOpen(open){
    nav.classList.toggle('nav-open',open);
    btn.setAttribute('aria-expanded',open);
    document.body.setAttribute('data-menu-open',open?'true':'');
  }
  btn.addEventListener('click',function(){setOpen(!nav.classList.contains('nav-open'));});
  document.querySelectorAll('.nav-links a').forEach(function(a){
    a.addEventListener('click',function(){setOpen(false);});
  });
})();
</script>
<script>${SEEKER_SCRIPT}</script>
</body>
</html>`;
}

// ── Pages ─────────────────────────────────────────────────

function homePage(state) {
  const live = state?._live ?? false;
  const thesis = state?.thesis ?? "The meaning of life is not yet known.";
  const cycle = state?.cycle ?? 0;
  const tensions = state?.activeTensionsCount ?? (state?.activeTensions ?? state?.insights ?? []).length;
  const coreClaims = state?.coreClaimsCount ?? 0;
  const shifts = (state?.paradigmShifts ?? []).length;
  const images = state?.totalImages ?? 0;
  const totalTokens = (state?.totalTokensIn ?? 0) + (state?.totalTokensOut ?? 0);
  const journals = listJournals();

  const statusHtml = live
    ? `<span class="live-dot"></span> Live &middot; Cycle ${fmtNum(cycle)} &middot; v${PKG_VERSION}`
    : `<span class="offline-dot"></span> Offline`;

  const currentPhase = state?.phase ?? null;
  const phasesOrder = ["EXPLORE", "SYNTHESIZE", "CRITIQUE", "EVOLVE"];
  const phaseStep = (key, label) => {
    const isCurrent = currentPhase === key;
    const varName = key.toLowerCase();
    return `<span class="phase-step${isCurrent ? " phase-current" : ""}" data-phase="${key}"><span class="phase-dot" style="background:var(--${varName})"></span>${label}</span>`;
  };

  const thesisRendered = renderThesisBold(thesis);
  const thesisFullEscaped = escapeHtml(thesis);

  // Find most recent journal entry that has an image
  const latestImageEntry = journals.find(j => j.thumbnailUrl);
  const latestImageHtml = latestImageEntry ? `
  <div class="latest-visual" id="latest-visual">
    <p class="section-intro">Once in a while, at a breakthrough moment or when it has something profound to express, <strong>it decides to generate an image</strong>.</p>
    <div class="latest-visual-card">
      <a href="/journal/${encodeURIComponent(latestImageEntry.file)}" class="latest-visual-link">
        <img src="${escapeHtml(latestImageEntry.thumbnailUrl)}" alt="${escapeHtml(latestImageEntry.title || "Latest visual reflection")}" class="latest-visual-img">
        <div class="latest-visual-overlay">
          <span class="nav-split-badge"><span class="split-cycle">C${latestImageEntry.cycle}</span><span class="split-phase" data-phase="${escapeHtml(latestImageEntry.phase || "")}">${escapeHtml(latestImageEntry.phase || "")}</span></span>
        </div>
      </a>
      <a href="/journal/${encodeURIComponent(latestImageEntry.file)}" class="latest-visual-caption">
        <span class="latest-visual-title">${escapeHtml(latestImageEntry.title || `Cycle ${latestImageEntry.cycle}`)}</span>
      </a>
    </div>
    <div class="latest-visual-actions">
      <a href="/gallery" class="btn btn-secondary">Gallery</a>
    </div>
  </div>` : "";

  const sliderEntries = journals.slice(0, 10).filter(j => j.finding || j.title || j.excerpt);
  const sliderCards = sliderEntries.map(j => {
    const text = escapeHtml(j.finding || j.title || j.excerpt);
    const dateStr = j.date ? fmtDate(j.date) : "";
    const timeStr = j.date ? fmtTimeUTC(j.date) : "";
    const confPct = j.confidence != null ? Math.round(j.confidence * 100) : null;
    const confHtml = confPct != null
      ? `<span class="slider-conf"><span class="slider-conf-track"><span class="slider-conf-fill" style="width:${confPct}%"></span></span>${confPct}%</span>`
      : "";
    const phaseAttr = j.phase && ["EXPLORE","SYNTHESIZE","CRITIQUE","EVOLVE"].includes(j.phase) ? ` data-phase="${j.phase}"` : "";
    const thumbHtml = j.thumbnailUrl
      ? `<img class="slider-card-thumb" src="${escapeHtml(j.thumbnailUrl)}" alt="" loading="lazy">`
      : "";
    return `<a href="/journal/${encodeURIComponent(j.file)}" class="slider-card">
      ${thumbHtml}
      <div class="slider-card-body">
        <div class="slider-quote">${text} <span class="slider-read-more">Read its thesis</span></div>
        <div class="slider-time">${dateStr}${timeStr ? " · " + timeStr : ""}</div>
        <div class="slider-meta">
          <span class="badge"${phaseAttr}>${j.phase}</span>
          <span>Cycle ${j.cycle}</span>
          ${confHtml}
        </div>
      </div>
    </a>`;
  }).join("");
  const sliderDots = sliderEntries.map((_, i) =>
    `<span class="slider-dot${i === 0 ? " active" : ""}"></span>`
  ).join("");
  const sliderHtml = sliderEntries.length >= 2 ? `
  <div class="findings-slider">
    <div class="findings-label">Recent Findings</div>
    <div class="slider-viewport" id="slider-viewport">
      <div class="slider-track" id="slider-track">${sliderCards}</div>
    </div>
    <div class="slider-nav">
      <button class="slider-btn" id="slider-prev" aria-label="Previous" disabled>${ARROW_LEFT}</button>
      <div class="slider-dots">${sliderDots}</div>
      <button class="slider-btn" id="slider-next" aria-label="Next">${ARROW_RIGHT}</button>
    </div>
  </div>` : "";

  return shell("Home", `
<div class="container">
  <div class="hero">
    <h1>The Meaning Seeker</h1>
    <p class="hero-question">Why are we here?</p>
    <p class="hero-tagline">An autonomous AI exploring life's deepest question — <strong>What is the meaning of life?</strong> — critiquing itself and evolving its answer, one cycle at a time.</p>
    <div class="phase-strip">
      ${phaseStep("EXPLORE", "Explore")}
      <span class="phase-arrow" aria-hidden="true">${ARROW_RIGHT_SM}</span>
      ${phaseStep("SYNTHESIZE", "Synthesize")}
      <span class="phase-arrow" aria-hidden="true">${ARROW_RIGHT_SM}</span>
      ${phaseStep("CRITIQUE", "Critique")}
      <span class="phase-arrow" aria-hidden="true">${ARROW_RIGHT_SM}</span>
      ${phaseStep("EVOLVE", "Evolve")}
      <span class="phase-arrow" aria-hidden="true">${ARROW_RIGHT_SM}</span>
      <span class="phase-strip-label">Loop</span>
    </div>
    <div class="status-line" id="status-line">${statusHtml}</div>
    <div class="status-line" id="next-round-line"></div>
  </div>

  <div class="thesis-label">Current answer</div>
  <div class="thesis-wrap" id="thesis-wrap" data-thesis-full="${thesisFullEscaped}">
    <div class="thesis-block">
      <span id="thesis">${thesisRendered}</span>
    </div>
    <p class="thesis-attribution" id="thesis-attribution">Found in Cycle ${fmtNum(cycle)}${state?._lastModified ? ", " + fmtDateTimeUTC(state._lastModified) : ""}</p>
    ${state?.confidence != null ? `<div class="confidence-line" id="confidence-line">
      <span id="confidence-text">Confidence ${Math.round((state.confidence ?? 0) * 100)}%</span>
      <span class="confidence-info-icon" tabindex="0" aria-label="About Confidence Level">&#x2139;</span>
      <div class="confidence-popup" role="tooltip">The <strong>Confidence Level</strong> is how confident the Seeker claims to be in its current thesis on the meaning of life. <strong>&ldquo;How sure am I about this thesis?&rdquo;</strong> &mdash; the AI&rsquo;s self-reported strength of belief.</div>
    </div>` : ""}
  </div>

  ${latestImageHtml}

  <div class="stats">
    <div class="stat">
      <div class="stat-value" id="s-cycle">${fmtNum(cycle)}</div>
      <div class="stat-label-wrap">
        <span class="stat-label">Cycles</span>
        <span class="confidence-info-icon" tabindex="0" aria-label="About Cycles">&#x2139;</span>
        <div class="confidence-popup" role="tooltip">Number of complete loops through Explore, Synthesize, Critique, and Evolve. Each cycle produces up to four journal entries.</div>
      </div>
    </div>
    <div class="stat stat-has-link">
      <a href="/insights#open-questions" class="stat-link"><div class="stat-value" id="s-tensions">${fmtNum(tensions)}</div></a>
      <div class="stat-label-wrap" id="stat-wrap-tensions">
        <a href="/insights#open-questions" class="stat-link stat-label">Open Questions</a>
        <span class="confidence-info-icon" tabindex="0" aria-label="About Open Questions">&#x2139;</span>
        <div class="confidence-popup has-link" role="tooltip">Unresolved tensions or contradictions the Seeker is still exploring — questions it has raised but not yet answered. <a href="/insights#open-questions">Read more</a></div>
      </div>
    </div>
    <div class="stat stat-has-link">
      <a href="/insights#core-claims" class="stat-link"><div class="stat-value" id="s-core-claims">${fmtNum(coreClaims)}</div></a>
      <div class="stat-label-wrap" id="stat-wrap-claims">
        <a href="/insights#core-claims" class="stat-link stat-label">Core Claims</a>
        <span class="confidence-info-icon" tabindex="0" aria-label="About Core Claims">&#x2139;</span>
        <div class="confidence-popup has-link" role="tooltip">Arguments the Seeker can defend that support its thesis — each with evidence, objections, and confidence. <a href="/insights#core-claims">Read more</a></div>
      </div>
    </div>
    <div class="stat stat-has-link">
      <a href="/insights#paradigm-shifts" class="stat-link"><div class="stat-value" id="s-shifts">${fmtNum(shifts)}</div></a>
      <div class="stat-label-wrap" id="stat-wrap-shifts">
        <a href="/insights#paradigm-shifts" class="stat-link stat-label">Paradigm Shifts</a>
        <span class="confidence-info-icon" tabindex="0" aria-label="About Paradigm Shifts">&#x2139;</span>
        <div class="confidence-popup has-link" role="tooltip">Moments when its understanding of the meaning of life changed fundamentally. <a href="/insights#paradigm-shifts">Read more</a></div>
      </div>
    </div>
    <div class="stat stat-has-link">
      <a href="/journal" class="stat-link"><div class="stat-value" id="s-entries">${fmtNum(journals.length)}</div></a>
      <div class="stat-label-wrap">
        <a href="/journal" class="stat-link stat-label">Entries</a>
        <span class="confidence-info-icon" tabindex="0" aria-label="About Entries">&#x2139;</span>
        <div class="confidence-popup has-link" role="tooltip">Diary entries in the journal — one per phase per cycle (Explore, Synthesize, Critique, Evolve). <a href="/journal">Read more</a></div>
      </div>
    </div>
    <div class="stat">
      <div class="stat-value" id="s-tokens">${tokenStr(totalTokens)}</div>
      <div class="stat-label-wrap">
        <span class="stat-label">Tokens</span>
        <span class="confidence-info-icon" tabindex="0" aria-label="About Tokens">&#x2139;</span>
        <div class="confidence-popup" role="tooltip">Total input and output tokens used so far by the thinking model (approximate).</div>
      </div>
    </div>
  </div>

  ${sliderHtml}

  <div class="cta-row">
    <a href="/journal" class="btn btn-primary">Read Its Thoughts</a>
    <a href="/how-to" class="btn btn-secondary">Run Your Own</a>
  </div>

  <script>window.__initialState = ${state && state._lastModifiedMs != null ? JSON.stringify({ _lastModifiedMs: state._lastModifiedMs, _cycleDelayMs: state._cycleDelayMs }) : "null"};</script>

  <div class="section" id="about">
    <div class="section-title">How it thinks</div>
    <p class="section-intro">
      The Meaning Seeker runs in a continuous loop, cycling through four phases. No human input. It writes, critiques itself, and evolves.
    </p>
    <div class="phase-grid">
      <a href="/journal?phase=EXPLORE" class="phase-item">
        <div class="phase-name"><span class="phase-dot" style="background:var(--explore)"></span>Explore</div>
        <div class="phase-desc">Dives deep into a philosophical domain — consciousness, ethics, love, death — seeking surprising insights.</div>
      </a>
      <a href="/journal?phase=SYNTHESIZE" class="phase-item">
        <div class="phase-name"><span class="phase-dot" style="background:var(--synthesize)"></span>Synthesize</div>
        <div class="phase-desc">Connects dots across explored domains, searching for unexpected bridges and a unifying thesis.</div>
      </a>
      <a href="/journal?phase=CRITIQUE" class="phase-item">
        <div class="phase-name"><span class="phase-dot" style="background:var(--critique)"></span>Critique</div>
        <div class="phase-desc">Articulates the strongest objections to its own thesis. Attacks from logic, empiricism, lived experience.</div>
      </a>
      <a href="/journal?phase=EVOLVE" class="phase-item">
        <div class="phase-name"><span class="phase-dot" style="background:var(--evolve)"></span>Evolve</div>
        <div class="phase-desc">Integrates everything and evolves the thesis. Paradigm shifts happen when understanding fundamentally changes.</div>
      </a>
    </div>
  </div>

  ${siteFooter("home")}
</div>

<script>
const PHASE_KEYS = ['EXPLORE','SYNTHESIZE','CRITIQUE','EVOLVE'];
const ARROW_LEFT_SVG = ${JSON.stringify(ARROW_LEFT)};
const ARROW_RIGHT_SVG = ${JSON.stringify(ARROW_RIGHT)};
let lastState = null;
let _sliderCur = 0;

function renderThesisBold(s) {
  return (s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
}

function updateThesisUI(full) {
  const wrap = document.getElementById('thesis-wrap');
  const el = document.getElementById('thesis');
  const attr = document.getElementById('thesis-attribution');
  if (!wrap || !el) return;
  const escaped = (full ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  wrap.setAttribute('data-thesis-full', escaped);
  el.innerHTML = renderThesisBold(full ?? '');
  if (attr) {
    let t = 'Found in Cycle ' + (lastState ? (lastState.cycle ?? 0).toLocaleString() : '0');
    if (lastState && lastState._lastModified) {
      const d = new Date(lastState._lastModified).toLocaleString('en-US', { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC' }) + ' UTC';
      t += ', ' + d;
    }
    attr.textContent = t;
  }
}

function _buildSliderCard(j) {
  const text = (j.finding || j.title || j.excerpt || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  const phaseAttr = j.phase && PHASE_KEYS.indexOf(j.phase) >= 0 ? ' data-phase="' + j.phase + '"' : '';
  let dateStr = '', timeStr = '';
  if (j.date) {
    const d = new Date(j.date);
    dateStr = d.toLocaleDateString('en-US', {year:'numeric',month:'short',day:'numeric'});
    timeStr = d.toLocaleTimeString('en-US', {timeZone:'UTC',hour:'2-digit',minute:'2-digit',hour12:false}) + ' UTC';
  }
  const confPct = j.confidence != null ? Math.round(j.confidence * 100) : null;
  const confHtml = confPct != null
    ? '<span class="slider-conf"><span class="slider-conf-track"><span class="slider-conf-fill" style="width:'+confPct+'%"></span></span>'+confPct+'%</span>'
    : '';
  return '<a href="/journal/' + encodeURIComponent(j.file) + '" class="slider-card">'
    + '<div class="slider-quote">' + text + ' <span class="slider-read-more">Read its thesis</span></div>'
    + '<div class="slider-time">' + dateStr + (timeStr ? ' \u00b7 ' + timeStr : '') + '</div>'
    + '<div class="slider-meta"><span class="badge"' + phaseAttr + '>' + j.phase + '</span>'
    + '<span>Cycle ' + j.cycle + '</span>' + confHtml + '</div></a>';
}

function _initSlider() {
  var track = document.getElementById('slider-track');
  if (!track || !track.children.length) return;
  var cards = Array.from(track.children), total = cards.length;
  if (total < 2) return;
  _sliderCur = 0;
  var oldPrev = document.getElementById('slider-prev');
  var oldNext = document.getElementById('slider-next');
  var prev = oldPrev ? oldPrev.cloneNode(true) : null;
  var next = oldNext ? oldNext.cloneNode(true) : null;
  if (prev && oldPrev) oldPrev.parentNode.replaceChild(prev, oldPrev);
  if (next && oldNext) oldNext.parentNode.replaceChild(next, oldNext);
  var dots = document.querySelectorAll('.slider-dot');
  function stride() { return cards[0].offsetWidth + 16; }
  function go(idx, anim) {
    _sliderCur = Math.max(0, Math.min(total - 1, idx));
    track.style.transition = anim !== false ? 'transform .45s cubic-bezier(.4,0,.2,1)' : 'none';
    track.style.transform = 'translateX(' + (-_sliderCur * stride()) + 'px)';
    for (var i = 0; i < dots.length; i++) dots[i].classList.toggle('active', i === _sliderCur);
    if (prev) prev.disabled = _sliderCur === 0;
    if (next) next.disabled = _sliderCur >= total - 1;
  }
  if (prev) prev.addEventListener('click', function() { go(_sliderCur - 1); });
  if (next) next.addEventListener('click', function() { go(_sliderCur + 1); });
  var sx = 0, sy = 0, dx = 0, dragging = false;
  track.addEventListener('touchstart', function(e) {
    sx = e.touches[0].clientX; sy = e.touches[0].clientY; dx = 0; dragging = true;
    track.style.transition = 'none';
  }, {passive: true});
  track.addEventListener('touchmove', function(e) {
    if (!dragging) return;
    dx = e.touches[0].clientX - sx;
    track.style.transform = 'translateX(' + (-_sliderCur * stride() + dx) + 'px)';
  }, {passive: true});
  track.addEventListener('touchend', function(e) {
    if (!dragging) return; dragging = false;
    var dy = e.changedTouches[0].clientY - sy;
    if (Math.abs(dx) > Math.abs(dy) && Math.abs(dx) > 40) { go(_sliderCur + (dx < 0 ? 1 : -1)); }
    else { go(_sliderCur); }
  });
  window.addEventListener('resize', function() { go(_sliderCur, false); });
  go(0, false);
}

async function _refreshSlider() {
  try {
    const r = await fetch('/api/journals');
    if (!r.ok) return;
    const journals = await r.json();
    const entries = journals.slice(0, 10).filter(j => j.finding || j.title || j.excerpt);
    if (entries.length < 2) return;
    let container = document.querySelector('.findings-slider');
    if (!container) {
      const ctaRow = document.querySelector('.cta-row');
      if (!ctaRow) return;
      container = document.createElement('div');
      container.className = 'findings-slider';
      ctaRow.parentNode.insertBefore(container, ctaRow);
    }
    const dots = entries.map((_, i) => '<span class="slider-dot' + (i === 0 ? ' active' : '') + '"></span>').join('');
    container.innerHTML = '<div class="findings-label">Recent Findings</div>'
      + '<div class="slider-viewport" id="slider-viewport"><div class="slider-track" id="slider-track">'
      + entries.map(_buildSliderCard).join('') + '</div></div>'
      + '<div class="slider-nav">'
      + '<button class="slider-btn" id="slider-prev" aria-label="Previous" disabled>' + ARROW_LEFT_SVG + '</button>'
      + '<div class="slider-dots">' + dots + '</div>'
      + '<button class="slider-btn" id="slider-next" aria-label="Next">' + ARROW_RIGHT_SVG + '</button></div>';
    _initSlider();
  } catch {}
}

async function refreshStats() {
  try {
    const r = await fetch('/api/state');
    if (!r.ok) return;
    const prevModMs = lastState?._lastModifiedMs;
    lastState = await r.json();
    const s = lastState;
    updateThesisUI(s.thesis ?? '');
    document.getElementById('s-cycle').textContent = (s.cycle ?? 0).toLocaleString();
    document.getElementById('s-tensions').textContent = (s.activeTensionsCount ?? (s.activeTensions ?? []).length ?? 0).toLocaleString();
    document.getElementById('s-core-claims').textContent = (s.coreClaimsCount ?? 0).toLocaleString();
    document.getElementById('s-shifts').textContent = ((s.paradigmShifts ?? []).length).toLocaleString();
    const t = (s.totalTokensIn ?? 0) + (s.totalTokensOut ?? 0);
    document.getElementById('s-tokens').textContent =
      t >= 1e6 ? (t/1e6).toFixed(1)+'M' : t >= 1e3 ? Math.round(t/1e3)+'K' : String(t);
    const el = document.getElementById('status-line');
    if (s._live) {
      el.innerHTML = '<span class="live-dot"></span> Live &middot; Cycle ' + (s.cycle ?? 0) + ' &middot; v${PKG_VERSION}';
    } else {
      el.innerHTML = '<span class="offline-dot"></span> Offline';
    }
    document.querySelectorAll('.phase-step').forEach(function(el) {
      el.classList.toggle('phase-current', el.dataset.phase === (s.phase || ''));
    });
    const confText = document.getElementById('confidence-text');
    const confLine = document.getElementById('confidence-line');
    if (s.confidence != null) {
      const pct = Math.round(s.confidence * 100);
      if (confText) confText.textContent = 'Confidence ' + pct + '%';
      if (confLine) confLine.style.display = '';
    } else {
      if (confLine) confLine.style.display = 'none';
    }
    if (prevModMs != null && s._lastModifiedMs !== prevModMs) {
      _refreshSlider();
    }
    updateNextRound();
  } catch {}
}
function updateNextRound() {
  const el = document.getElementById('next-round-line');
  if (!el) return;
  const s = lastState;
  if (!s || s._lastModifiedMs == null || s._cycleDelayMs == null) {
    el.textContent = '';
    return;
  }
  const nextAt = s._lastModifiedMs + s._cycleDelayMs;
  const remain = Math.max(0, Math.ceil((nextAt - Date.now()) / 1000));
  if (remain > 0) {
    const m = Math.floor(remain / 60);
    const sec = remain % 60;
    el.textContent = 'Next phase in ' + (m ? m + 'm ' : '') + sec + 's';
    el.style.color = 'var(--text3)';
    el.classList.remove('next-round-link-color');
  } else {
    el.textContent = 'Next phase: any moment\u2026';
    el.style.color = '';
    el.classList.add('next-round-link-color');
  }
}
setInterval(refreshStats, 5000);
setInterval(updateNextRound, 1000);
if (window.__initialState) lastState = window.__initialState;
updateNextRound();
refreshStats();
(function() {
  const line = document.getElementById('confidence-line');
  if (line) {
    line.addEventListener('click', function(e) {
      line.classList.toggle('popup-open');
      e.stopPropagation();
    });
    line.querySelector('.confidence-info-icon')?.addEventListener('keydown', function(e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); line.classList.toggle('popup-open'); }
    });
  }
  document.addEventListener('click', function() {
    document.getElementById('confidence-line')?.classList.remove('popup-open');
    document.querySelectorAll('.stat-label-wrap').forEach(function(w) { w.classList.remove('popup-open'); });
  });
  document.querySelectorAll('.stat-label-wrap').forEach(function(wrap) {
    const icon = wrap.querySelector('.confidence-info-icon');
    if (!icon) return;
    icon.addEventListener('click', function(e) { e.preventDefault(); e.stopPropagation(); wrap.classList.toggle('popup-open'); });
    icon.addEventListener('keydown', function(e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); wrap.classList.toggle('popup-open'); }
    });
  });
})();
_initSlider();
</script>
`, "home", "An autonomous AI exploring life's deepest question — What is the meaning of life? — critiquing itself and evolving its answer, one cycle at a time.");
}

function journalListPage(journals) {
  const journalsForClient = journals.map((j) => ({
    file: j.file,
    cycle: j.cycle,
    phase: j.phase,
    model: j.model,
    date: j.date?.toISOString() ?? null,
    excerpt: j.excerpt,
    title: j.title ?? null,
    hasImage: j.hasImage,
    thumbnailUrl: j.thumbnailUrl ?? null,
  }));

  return shell(`Journal`, `
<div class="container">
  <div style="padding-top:48px">
    <h1 style="font-size:22px;font-weight:600;letter-spacing:-.02em;margin-bottom:24px">
      Journal
      <span style="color:var(--text3);font-weight:400;font-size:15px" id="journal-count">(${journals.length} entries)</span>
    </h1>

    <div class="filter-bar">
      <button class="filter-btn active" data-phase="ALL" onclick="setFilter(this)">All</button>
      <button class="filter-btn" data-phase="EXPLORE" style="--c:var(--explore)" onclick="setFilter(this)">Explore</button>
      <button class="filter-btn" data-phase="SYNTHESIZE" style="--c:var(--synthesize)" onclick="setFilter(this)">Synthesize</button>
      <button class="filter-btn" data-phase="CRITIQUE" style="--c:var(--critique)" onclick="setFilter(this)">Critique</button>
      <button class="filter-btn" data-phase="EVOLVE" style="--c:var(--evolve)" onclick="setFilter(this)">Evolve</button>
    </div>

    <div id="journal-list"></div>
    <div class="empty-state" id="journal-empty" style="display:none">
      <p>No journal entries yet.</p>
      <p style="margin-top:8px;font-size:14px">Start the seeker with <code>yarn start</code></p>
    </div>
  </div>
</div>
<script>
const PHASE_KEYS = ['EXPLORE','SYNTHESIZE','CRITIQUE','EVOLVE'];
const journals = ${JSON.stringify(journalsForClient)};

function toLocalDateKey(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}

function dayLabel(dateKey) {
  const today = toLocalDateKey(new Date().toISOString());
  const yesterday = (() => { const d = new Date(); d.setDate(d.getDate() - 1); return toLocalDateKey(d.toISOString()); })();
  if (dateKey === today) return 'Today';
  if (dateKey === yesterday) return 'Yesterday';
  const [y, m, d] = dateKey.split('-');
  const date = new Date(y, m - 1, d);
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function formatTimeUTC(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleTimeString('en-US', { timeZone: 'UTC', hour: '2-digit', minute: '2-digit', hour12: false }) + ' UTC';
}

function formatTimeLocal(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
}

function renderJournalList() {
  const todayKey = toLocalDateKey(new Date().toISOString());
  const byDay = {};
  journals.forEach(j => {
    const key = toLocalDateKey(j.date);
    if (!byDay[key]) byDay[key] = [];
    byDay[key].push(j);
  });
  const dayKeys = Object.keys(byDay).sort((a, b) => b.localeCompare(a));
  // Today first
  if (dayKeys.includes(todayKey)) {
    dayKeys.splice(dayKeys.indexOf(todayKey), 1);
    dayKeys.unshift(todayKey);
  }

  const listEl = document.getElementById('journal-list');
  const emptyEl = document.getElementById('journal-empty');
  if (dayKeys.length === 0) {
    listEl.innerHTML = '';
    emptyEl.style.display = 'block';
    return;
  }
  emptyEl.style.display = 'none';

  listEl.innerHTML = dayKeys.map(dateKey => {
    const entries = byDay[dateKey];
    const isToday = dateKey === todayKey;
    const cards = entries.map(j => {
      const phaseAttr = j.phase && PHASE_KEYS.indexOf(j.phase) >= 0 ? ' data-phase="' + j.phase + '"' : '';
      const thumb = j.thumbnailUrl
        ? '<img class="journal-card-thumb" src="' + j.thumbnailUrl + '" alt="" loading="lazy">'
        : '';
      const timeHtml = j.date
        ? '<time class="card-date" datetime="' + j.date + '">' + formatTimeUTC(j.date) + ' <span class="card-date-local">· ' + formatTimeLocal(j.date) + '</span></time>'
        : '';
      var exc = (j.excerpt || '<em>(empty entry)</em>').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\"/g, '&quot;');
      var titleHtml = (j.title && j.title.trim()) ? '<div class="card-title">' + j.title.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\"/g, '&quot;') + '</div>' : '';
      return '<div class="journal-card" data-phase="' + j.phase + '">' + thumb + '\\n  <a href=\"/journal/' + encodeURIComponent(j.file) + '\">\\n    <div style=\"flex:1;min-width:0\">\\n      <div class=\"card-meta\">\\n        <span class=\"badge\"' + phaseAttr + '>' + j.phase + '</span>\\n        <span class=\"card-model\">Cycle ' + j.cycle + ' · ' + (j.model || '') + (j.hasImage ? ' \u00a0\u00a0\u00a0\uD83C\uDFA8' : '') + '</span>' + timeHtml + '\\n      </div>\\n      ' + titleHtml + '\\n      <div class=\"card-excerpt\">' + exc + '</div>\\n    </div>\\n  </a>\\n</div>';
    }).join('\\n');
    return '<details class="journal-day" data-day="' + dateKey + '"' + (isToday ? ' open' : '') + '><summary>' + dayLabel(dateKey) + ' (' + entries.length + ')</summary><div class="day-entries">' + cards + '</div></details>';
  }).join('\\n');
}

renderJournalList();

function setFilter(btn) {
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  const phase = btn.dataset.phase;
  document.querySelectorAll('.journal-card').forEach(card => {
    card.style.display = (phase === 'ALL' || card.dataset.phase === phase) ? '' : 'none';
  });
}

(function applyPhaseFromUrl() {
  const params = new URLSearchParams(location.search);
  const phase = params.get('phase');
  if (phase && ['EXPLORE','SYNTHESIZE','CRITIQUE','EVOLVE'].indexOf(phase) !== -1) {
    const btn = document.querySelector('.filter-btn[data-phase="' + phase + '"]');
    if (btn) setFilter(btn);
  }
})();
</script>
`, "journal", "Browse the Seeker's philosophical diary — each cycle's explorations, syntheses, critiques, and evolved theses on the meaning of life.");
}

function formatGalleryDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const dateStr = d.toLocaleDateString("en-US", { day: "numeric", month: "short", year: "numeric" });
  const timeStr = d.toLocaleTimeString("en-US", { timeZone: "UTC", hour: "2-digit", minute: "2-digit", hour12: false }) + " UTC";
  return dateStr + ", " + timeStr;
}

function galleryPage(items) {
  const body =
    items.length === 0
      ? `
<div class="container" style="padding-top:48px">
  <div class="gallery-empty">
    <p>No images yet</p>
    <p>Generated images from the seeker's journey will appear here when it creates visual reflections.</p>
    <p style="margin-top:16px;font-size:14px">Run the seeker with an image model (e.g. <code>yarn start</code> and choose an image model) to see art in the gallery.</p>
  </div>
</div>`
      : `
<div class="container" style="padding-top:48px">
  <div style="margin-bottom:32px">
    <h1 style="font-size:28px;font-weight:700;letter-spacing:-.025em;margin-bottom:8px">Gallery</h1>
    <p style="font-size:15px;color:var(--text2)">Generated images from the Seeker's journey (${items.length} ${items.length === 1 ? "image" : "images"})</p>
  </div>
  <script type="application/json" id="gallery-items">${JSON.stringify(items.map((it) => ({ imageUrl: it.imageUrl, title: it.title, prompt: it.prompt, cycle: it.cycle, phase: it.phase, date: it.date })))}</script>
  <div id="gallery-modal" class="gallery-modal" role="dialog" aria-modal="true" aria-label="Image viewer">
    <div class="gallery-modal-top">
      <span class="gallery-modal-counter" id="gallery-modal-counter"></span>
      <button type="button" class="gallery-modal-close" onclick="window.galleryModalClose()" aria-label="Close">×</button>
    </div>
    <div class="gallery-modal-inner">
      <div class="gallery-modal-side-nav">
        <button type="button" class="gallery-modal-prev" onclick="window.galleryModalPrev()" aria-label="Previous image">${ARROW_LEFT}</button>
      </div>
      <div class="gallery-modal-center">
        <div class="gallery-modal-img-wrap" id="gallery-modal-img-wrap">
          <img id="gallery-modal-img" src="" alt="">
        </div>
        <div class="gallery-modal-title" id="gallery-modal-title"></div>
        <div class="gallery-modal-nav-row">
          <button type="button" class="gallery-modal-prev" onclick="window.galleryModalPrev()" aria-label="Previous image">${ARROW_LEFT}</button>
          <button type="button" class="gallery-modal-next" onclick="window.galleryModalNext()" aria-label="Next image">${ARROW_RIGHT}</button>
        </div>
      </div>
      <div class="gallery-modal-side-nav">
        <button type="button" class="gallery-modal-next" onclick="window.galleryModalNext()" aria-label="Next image">${ARROW_RIGHT}</button>
      </div>
    </div>
  </div>
  <div class="gallery-grid">
    ${items
      .map((it, idx) => {
        const phaseAttr = it.phase && ["EXPLORE", "SYNTHESIZE", "CRITIQUE", "EVOLVE"].includes(it.phase) ? ` data-phase="${it.phase}"` : "";
        const promptEscaped = escapeHtml(it.prompt || "—");
        const promptId = "gallery-prompt-" + (it.imageUrl || "").replace(/\W/g, "-");
        const journalLink =
          it.journalFile ?
            `<a href="/journal/${encodeURIComponent(it.journalFile)}" style="font-size:12px;color:var(--accent);margin-left:auto">View entry</a>`
          : "";
        return `
    <article class="gallery-card" data-gallery-index="${idx}">
      <div class="gallery-card-img-wrap">
        <button type="button" class="gallery-card-img-btn" data-gallery-index="${idx}" aria-label="View full size">
          <img src="${escapeHtml(it.imageUrl)}" alt="" loading="lazy">
        </button>
      </div>
      <div class="gallery-card-meta">
        <span style="font-size:13px;font-weight:600;color:var(--text)">Cycle ${it.cycle}</span>
        <span class="badge"${phaseAttr}>${escapeHtml(it.phase || "—")}</span>
        <time datetime="${escapeHtml(it.date)}" style="font-size:13px;color:var(--text2);font-variant-numeric:tabular-nums">${escapeHtml(formatGalleryDate(it.date))}</time>
        ${journalLink}
      </div>
      ${it.title ? `<div class="gallery-card-title">${escapeHtml(it.title)}</div>` : ""}
      <div class="gallery-card-prompt" id="${promptId}">${promptEscaped}</div>
      ${(it.prompt || "").length > 120 ? `<button type="button" class="gallery-card-prompt-toggle" onclick="var el=document.getElementById('${promptId}');el.classList.toggle('expanded');this.textContent=el.classList.contains('expanded')?'Show less':'Show more'">Show more</button>` : ""}
    </article>`;
      })
      .join("")}
  </div>
</div>
<script>
(function(){
  var script=document.getElementById('gallery-items');
  if(!script)return;
  var items=JSON.parse(script.textContent);
  var modal=document.getElementById('gallery-modal');
  var imgEl=document.getElementById('gallery-modal-img');
  var imgWrap=document.getElementById('gallery-modal-img-wrap');
  var titleEl=document.getElementById('gallery-modal-title');
  var counterEl=document.getElementById('gallery-modal-counter');
  var idx=0;
  function show(){if(!modal||!imgEl||idx<0||idx>=items.length)return;var it=items[idx];imgEl.src=it.imageUrl;imgEl.alt=it.title||'';modal.classList.add('open');document.body.style.overflow='hidden';if(counterEl)counterEl.textContent=(idx+1)+' / '+items.length;if(titleEl){titleEl.textContent=it.title||'';titleEl.style.display=it.title?'block':'none';}}
  function hide(){if(modal){modal.classList.remove('open');document.body.style.overflow='';}}
  window.galleryModalOpen=function(i){idx=((i%items.length)+items.length)%items.length;show();};
  window.galleryModalClose=hide;
  window.galleryModalPrev=function(){idx=(idx-1+items.length)%items.length;show();};
  window.galleryModalNext=function(){idx=(idx+1)%items.length;show();};
  document.querySelectorAll('.gallery-card-img-btn').forEach(function(btn){btn.addEventListener('click',function(){var i=parseInt(btn.getAttribute('data-gallery-index'),10);if(!isNaN(i))window.galleryModalOpen(i);});});
  var lastSwipeTime=0;
  if(imgWrap){imgWrap.addEventListener('click',function(e){if(Date.now()-lastSwipeTime<400)return;window.galleryModalNext();});}
  modal.addEventListener('click',function(e){if(e.target===modal)hide();});
  document.addEventListener('keydown',function(e){if(!modal.classList.contains('open'))return;if(e.key==='Escape')hide();else if(e.key==='ArrowLeft')window.galleryModalPrev();else if(e.key==='ArrowRight')window.galleryModalNext();});
  var touchStartX=0;
  if(modal){modal.addEventListener('touchstart',function(e){touchStartX=e.changedTouches[0].screenX;},{passive:true});modal.addEventListener('touchend',function(e){var touchEndX=e.changedTouches[0].screenX,dx=touchEndX-touchStartX;if(Math.abs(dx)>50){lastSwipeTime=Date.now();if(dx>0)window.galleryModalPrev();else window.galleryModalNext();}},{passive:true});}
})();
</script>`;
  return shell("Gallery", body, "gallery", "AI-generated images born from moments when language failed — visual expressions of the Meaning Seeker's philosophical journey.");
}

function journalsForCycle(journals, cycle) {
  if (cycle == null) return [];
  return journals.filter((j) => j.cycle === cycle);
}

function insightJournalEntryLink(j) {
  const phase = j.phase || "";
  const phaseAttr = phase && ["EXPLORE", "SYNTHESIZE", "CRITIQUE", "EVOLVE"].includes(phase) ? ` data-phase="${escapeHtml(phase)}"` : "";
  const badge = `<span class="nav-split-badge"><span class="split-cycle">C${j.cycle}</span><span class="split-phase"${phaseAttr}>${escapeHtml(phase)}</span></span>`;
  const rawTitle = (j.title && j.title.trim()) || j.excerpt || "";
  const title = rawTitle.length > 80 ? rawTitle.slice(0, 77) + "…" : rawTitle;
  return `<a href="/journal/${encodeURIComponent(j.file)}" class="nav-arrow nav-arrow--prev"><span class="nav-arrow-top">${badge}</span><span class="nav-arrow-title">${escapeHtml(title)}</span></a>`;
}

function insightJournalExpandable(cycleJournals) {
  if (cycleJournals.length === 0) return "";
  const summaryLabel = cycleJournals.length === 1 ? "Journal (1 entry)" : `Journal (${cycleJournals.length} entries)`;
  const entries = cycleJournals.map((j) => insightJournalEntryLink(j)).join("");
  return `<details class="journal-day insight-journal-details"><summary>${summaryLabel}</summary><div class="day-entries">${entries}</div></details>`;
}

function insightsPage(state, journals) {
  const tensions = state?.activeTensions ?? [];
  const claims = state?.core_claims ?? [];
  const shifts = state?.paradigmShifts ?? [];

  const section = (id, title, intro, itemsHtml) => `
  <section class="section" id="${id}">
    <h2 class="section-title">${title}</h2>
    <p class="section-intro" style="text-align:left;max-width:none">${intro}</p>
    ${itemsHtml}
  </section>`;

  const openQuestionsHtml =
    tensions.length === 0
      ? `<p style="color:var(--text2);font-size:15px">No open questions yet. They appear as the Seeker explores and raises tensions it has not yet resolved.</p>`
      : tensions
          .map((t) => {
            const cycle = t.created_cycle;
            const cycleJournals = journalsForCycle(journals, cycle);
            const journalLinks = insightJournalExpandable(cycleJournals);
            return `<div class="insight-item">
  <h3>${escapeHtml(t.tension || "—")}</h3>
  <div class="insight-meta">${t.domains?.length ? escapeHtml(t.domains.join(" · ")) : ""} ${cycle != null ? " · Cycle " + cycle : ""}</div>
  ${journalLinks}
</div>`;
          })
          .join("");

  const coreClaimsHtml =
    claims.length === 0
      ? `<p style="color:var(--text2);font-size:15px">No core claims yet. They form as the Seeker synthesizes and evolves its thesis.</p>`
      : claims
          .map((c) => {
            const cycle = c.first_articulated_cycle;
            const cycleJournals = journalsForCycle(journals, cycle);
            const journalLinks = insightJournalExpandable(cycleJournals);
            const confPct = typeof c.confidence === "number" ? Math.round(c.confidence * 100) : null;
            const evidence = Array.isArray(c.supporting_evidence) ? c.supporting_evidence : [];
            const evidenceHtml = evidence.length ? `<p style="font-size:14px;color:var(--text2);margin:8px 0"><strong>Evidence:</strong> ${escapeHtml(evidence.join("; "))}</p>` : "";
            const objectionHtml = c.strongest_objection ? `<p style="font-size:14px;color:var(--text2);margin:8px 0"><strong>Objection:</strong> ${escapeHtml(c.strongest_objection)}</p>` : "";
            return `<div class="insight-item">
  <h3>${escapeHtml(c.claim || "—")}</h3>
  <div class="insight-meta">${confPct != null ? confPct + "% confidence" : ""} ${c.domain_roots?.length ? " · " + escapeHtml(c.domain_roots.join(", ")) : ""} ${cycle != null ? " · Cycle " + cycle : ""}</div>
  ${evidenceHtml}
  ${objectionHtml}
  ${journalLinks}
</div>`;
          })
          .join("");

  const paradigmShiftsHtml =
    shifts.length === 0
      ? `<p style="color:var(--text2);font-size:15px">No paradigm shifts yet. They are recorded when the Seeker's understanding of the meaning of life changes fundamentally.</p>`
      : shifts
          .map((s) => {
            const cycle = s.cycle;
            const cycleJournals = journalsForCycle(journals, cycle);
            const journalLinks = insightJournalExpandable(cycleJournals);
            return `<div class="insight-item">
  <h3>${escapeHtml(s.description || "Paradigm shift")}</h3>
  <div class="insight-meta">Cycle ${cycle ?? "—"}</div>
  ${journalLinks}
</div>`;
          })
          .join("");

  const body = `
<div class="container" style="padding-top:48px">
  <div style="margin-bottom:40px">
    <h1 style="font-size:28px;font-weight:700;letter-spacing:-.025em;margin-bottom:8px">Insights</h1>
    <p style="font-size:15px;color:var(--text2)"><a href="#open-questions">Open questions</a>, <a href="#core-claims">core claims</a>, and <a href="#paradigm-shifts">paradigm shifts</a> from the Seeker's journey.</p>
  </div>
  ${section("open-questions", "Open Questions", "Unresolved tensions or contradictions the Seeker is still exploring.", openQuestionsHtml)}
  ${section("core-claims", "Core Claims", "Arguments the Seeker can defend that support its thesis.", coreClaimsHtml)}
  ${section("paradigm-shifts", "Paradigm Shifts", "Moments when its understanding of the meaning of life changed fundamentally.", paradigmShiftsHtml)}
</div>`;

  return shell("Insights", body, "insights", "Open questions, core claims, and paradigm shifts from the Meaning Seeker's philosophical journey.");
}

function howToPage() {
  return shell("How to run", `
<div class="container how-to-run" style="padding-top:48px">
  <div style="margin-bottom:48px">
    <h1 style="font-size:32px;font-weight:700;letter-spacing:-.025em;margin-bottom:8px">How to run Meaning Seeker</h1>
    <p style="font-size:17px;color:var(--text2)">Run it yourself on any model &mdash; no lock-in</p>
    <p style="font-size:15px;color:var(--text2);margin-top:12px">
      <a href="https://github.com/matiyin/meaning-seeker.com" target="_blank" rel="noopener noreferrer" style="color:var(--text);text-decoration:none;display:inline-flex;align-items:center;gap:6px">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/></svg>
        GitHub repository
      </a>
    </p>
  </div>

  <div class="section" style="padding-top:0">
    <div class="section-title">What it runs on</div>
    <p style="color:var(--text2);font-size:15px;line-height:1.65">
      Meaning Seeker runs <strong style="color:var(--text)">on your computer</strong> (or a server you control). It uses <strong style="color:var(--text)">Node.js</strong> and <strong style="color:var(--text)">libraries</strong> you install with <code>yarn install</code>; the app runs locally. You can use <strong style="color:var(--text)">cloud AI services</strong> (with API keys) or <strong style="color:var(--text)">local models via Ollama</strong> &mdash; no keys required, everything stays on your machine. You pick the model, so there's no lock-in to one company.
    </p>
  </div>

  <div class="section">
    <div class="section-title">Quick start</div>
    <pre><code>yarn install</code></pre>
    <p style="color:var(--text2);font-size:15px;margin:12px 0 16px">
      Put API keys in a <code>.env</code> file (or export them). The app loads <code>.env</code> automatically:
    </p>
    <pre><code>OPENROUTER_API_KEY=sk-or-...
ANTHROPIC_API_KEY=sk-ant-...
# etc. — see Config and model tables below</code></pre>
    <pre style="margin-top:16px"><code>yarn start</code></pre>
    <p style="color:var(--text2);font-size:15px;margin:12px 0">
      <strong style="color:var(--text)">CLI only</strong> &mdash; <code>yarn start</code> runs the Seeker in your terminal (you'll pick a thinking model, then an image model). It does <strong style="color:var(--text)">not</strong> start the website. To view the journal and thesis in a browser, use PM2 to run both the Seeker and the web server (see <strong style="color:var(--text)">Run in the browser with PM2</strong> below).
    </p>
  </div>

  <div class="section">
    <div class="section-title">Running, stopping, resetting</div>
    <p style="color:var(--text2);font-size:15px;line-height:1.65;margin-bottom:12px">
      <strong style="color:var(--text)">What you see</strong> &mdash; After each phase header you'll see "Calling model&hellip;" while it waits for the API. For response timings, run with <code>DEBUG=1 yarn start</code>.
    </p>
    <p style="color:var(--text2);font-size:15px;line-height:1.65;margin-bottom:12px">
      <strong style="color:var(--text)">Stop seeker (Ctrl+C)</strong> &mdash; State is saved. When you run again, it continues from the same cycle and thesis; you'll choose models again (or pass them on the CLI).
    </p>
    <p style="color:var(--text2);font-size:15px;line-height:1.65;margin-bottom:12px">
      <strong style="color:var(--text)">Stop web UI</strong> &mdash; If you started it with <code>yarn web</code>, stop it with <strong>Ctrl+C</strong> in that terminal. If it's running under PM2: <code>pm2 stop meaning-seeker-web</code> (restart with <code>pm2 start meaning-seeker-web</code>; remove from PM2 with <code>pm2 delete meaning-seeker-web</code>).
    </p>
    <p style="color:var(--text2);font-size:15px;line-height:1.65;margin-bottom:12px">
      <strong style="color:var(--text)">Find where the web UI is running</strong> &mdash; If you're not sure: <code>lsof -i :3000</code> (or <code>WEB_PORT</code> if set) shows the process on the default port; look for <code>node web.mjs</code> and note the PID. Stop it with <code>kill &lt;PID&gt;</code>. With PM2: <code>pm2 list</code> to see if <code>meaning-seeker-web</code> is there, then <code>pm2 stop meaning-seeker-web</code>.
    </p>
    <p style="color:var(--text2);font-size:15px;line-height:1.65">
      <strong style="color:var(--text)">Reset</strong> &mdash; Start over from cycle 0: <code>yarn reset</code> or <code>node seeker.mjs --reset</code>. This only removes <code>data/state.json</code>; journal and ledger stay. To wipe everything: <code>rm -rf data/</code>.
    </p>
  </div>

  <div class="section">
    <div class="section-title">Confidence level (%)</div>
    <p style="color:var(--text2);font-size:15px;line-height:1.65">
      The <strong style="color:var(--text)">confidence percentage</strong> is how confident the Seeker claims to be in its <strong style="color:var(--text)">current thesis on the meaning of life</strong>. The thinking model outputs a value from 0 to 1 in the SYNTHESIZE and EVOLVE phases; the app stores it and displays it as 0&ndash;100%. Meaning: "How sure am I about this thesis?" &mdash; the AI's self-reported strength of belief. Not a statistical confidence interval.
    </p>
  </div>

  <div class="section">
    <div class="section-title">CLI shortcut</div>
    <pre><code>node seeker.mjs claude-haiku flux-schnell-free
node seeker.mjs claude-cli none
node seeker.mjs kimi-k2.5 none
node seeker.mjs ollama flux-pro</code></pre>
  </div>

  <div class="section">
    <div class="section-title">Image generation</div>
    <p style="color:var(--text2);font-size:15px;line-height:1.65;margin-bottom:12px">
      <strong style="color:var(--text)">1. AI-decided (default)</strong> &mdash; The model outputs a structured JSON decision at the end of each diary entry with <code>wants_image: true/false</code> and an <code>image_motivation</code> explaining why. Images are generated only when: language is failing (pre-verbal, spatial, paradoxical); two contradictions are both true (paradox); something genuinely new emerged; or the moment is foundational. Every decision (yes and no) is logged to <code>data/image-decisions/</code>. The seeker also reflects on its own past images &mdash; recent prompts and motivations are fed back into context.
    </p>
    <p style="color:var(--text2);font-size:15px;line-height:1.65;margin-bottom:12px">
      <strong style="color:var(--text)">2. Interval-based</strong> &mdash; Set <code>IMAGE_EVERY_N_CYCLES=3</code> to force an image every N cycles during the EVOLVE phase. The prompt is auto-generated from the current thesis.
    </p>
    <p style="color:var(--text2);font-size:15px;line-height:1.65">
      <strong style="color:var(--text)">3. Fallback</strong> &mdash; Models that don't handle structured JSON well can still use the legacy <code>IMAGE_PROMPT:</code> marker. The parser tries structured JSON first, then bare JSON, then the legacy format. Images are saved to <code>data/images/</code> and embedded in the markdown journal.
    </p>
  </div>

  <div class="section">
    <div class="section-title">God mode</div>
    <pre><code>yarn god</code></pre>
    <p style="color:var(--text2);font-size:15px;margin-top:8px">Send directives, read the thesis, view journal entries, check stats.</p>
  </div>

  <div class="section">
    <div class="section-title">Thinking models</div>
    <table>
      <thead><tr><th>Key</th><th>Model</th><th>Provider</th><th>Notes</th></tr></thead>
      <tbody>
        <tr><td><code>claude-opus</code></td><td>Claude Opus 4.5</td><td>Anthropic</td><td>Most capable. Expensive for 24/7.</td></tr>
        <tr><td><code>claude-sonnet</code></td><td>Claude Sonnet 4.5</td><td>Anthropic</td><td>Great balance of quality and cost.</td></tr>
        <tr><td><code>claude-haiku</code></td><td>Claude Haiku 4.5</td><td>Anthropic</td><td>Cheapest Anthropic option for 24/7.</td></tr>
        <tr><td><code>claude-cli</code></td><td>Claude CLI (default)</td><td>Local</td><td>Uses local <code>claude</code>; env <code>CLAUDE_CLI_MODEL</code></td></tr>
        <tr><td><code>claude-cli-opus</code></td><td>Claude CLI — Opus</td><td>Local</td><td><code>claude --model opus</code></td></tr>
        <tr><td><code>claude-cli-sonnet</code></td><td>Claude CLI — Sonnet</td><td>Local</td><td><code>claude --model sonnet</code></td></tr>
        <tr><td><code>claude-cli-haiku</code></td><td>Claude CLI — Haiku</td><td>Local</td><td><code>claude --model haiku</code></td></tr>
        <tr><td><code>kimi-k2.5</code></td><td>Kimi K2.5</td><td>Moonshot</td><td>Open source multimodal MoE. Cheap.</td></tr>
        <tr><td><code>kimi-k2.5-nvidia</code></td><td>Kimi K2.5</td><td>NVIDIA</td><td>Free tier via NVIDIA NIM. Rate limits may apply.</td></tr>
        <tr><td><code>openrouter-claude-opus</code></td><td>Claude Opus 4.6</td><td><a href="https://openrouter.ai/anthropic/claude-opus-4.6/pricing" target="_blank" rel="noopener">OpenRouter</a></td><td>Strongest for coding &amp; long tasks. ~$1/day hourly cycle.</td></tr>
        <tr><td><code>openrouter-kimi</code></td><td>Kimi K2.5</td><td><a href="https://openrouter.ai/moonshotai/kimi-k2.5/pricing" target="_blank" rel="noopener">OpenRouter</a></td><td>Pay-per-token.</td></tr>
        <tr><td><code>openrouter-gemini</code></td><td>Gemini 2.5 Pro</td><td><a href="https://openrouter.ai/google/gemini-2.5-pro-preview/pricing" target="_blank" rel="noopener">OpenRouter</a></td><td>Google's reasoning model.</td></tr>
        <tr><td><code>openrouter-llama</code></td><td>Llama 4 Maverick</td><td><a href="https://openrouter.ai/meta-llama/llama-4-maverick/pricing" target="_blank" rel="noopener">OpenRouter</a></td><td>Meta's open model.</td></tr>
        <tr><td><code>openrouter-step-3.5-flash</code></td><td>Step 3.5 Flash</td><td><a href="https://openrouter.ai/stepfun/step-3.5-flash/pricing" target="_blank" rel="noopener">OpenRouter</a></td><td>256k context, free tier.</td></tr>
        <tr><td><code>together-kimi</code></td><td>Kimi K2.5</td><td>Together AI</td><td>Pay-per-token.</td></tr>
        <tr><td><code>ollama</code></td><td>Local model</td><td>Ollama</td><td>Free, runs locally. Needs GPU.</td></tr>
      </tbody>
    </table>
    <p style="color:var(--text3);font-size:13px;margin-top:8px">Add more in <code>models.mjs</code> or <code>image-models.mjs</code>.</p>
  </div>

  <div class="section">
    <div class="section-title">Image models</div>
    <table>
      <thead><tr><th>Key</th><th>Model</th><th>Notes</th></tr></thead>
      <tbody>
        <tr><td><code>flux-schnell-free</code></td><td>FLUX.1 Schnell</td><td>Free via Together AI. Fast, good quality.</td></tr>
        <tr><td><code>flux-schnell</code></td><td>FLUX.1 Schnell</td><td>Paid, fast, 4 steps. Cheapest paid option.</td></tr>
        <tr><td><code>flux-pro</code></td><td>FLUX.1 Pro</td><td>High quality, 28 steps.</td></tr>
        <tr><td><code>flux2-max</code></td><td>FLUX.2 Max</td><td>Best quality. Most expensive.</td></tr>
        <tr><td><code>flux2-klein-4b</code></td><td>FLUX.2 Klein 4B</td><td><a href="https://openrouter.ai/black-forest-labs/flux.2-klein-4b/pricing" target="_blank" rel="noopener">OpenRouter</a> — Fast, cost-effective. ~$0.01/image.</td></tr>
        <tr><td><code>flux2-flex</code></td><td>FLUX.2 Flex</td><td><a href="https://openrouter.ai/black-forest-labs/flux.2-flex/pricing" target="_blank" rel="noopener">OpenRouter</a> — Text, typography, fine details.</td></tr>
        <tr><td><code>dall-e-3</code></td><td>DALL·E 3</td><td>OpenAI's image model.</td></tr>
        <tr><td><code>gpt-image</code></td><td>GPT Image 1.5</td><td>Latest OpenAI image model.</td></tr>
        <tr><td><code>none</code></td><td>Disabled</td><td>Text-only journal.</td></tr>
      </tbody>
    </table>
  </div>

  <div class="section">
    <div class="section-title">Config</div>
    <table>
      <thead><tr><th>Env variable</th><th>Default</th><th>Description</th></tr></thead>
      <tbody>
        <tr><td><code>CYCLE_DELAY_MS</code></td><td>300000</td><td>Delay between phases (ms). Used by web for next-round countdown.</td></tr>
        <tr><td><code>CYCLE_DELAY_SECONDS</code></td><td>—</td><td>Same as above, in seconds. When set, seeker skips the interval prompt (e.g. for PM2).</td></tr>
        <tr><td><code>COMPACTION_LEVEL</code></td><td>—</td><td><code>none</code> | <code>low</code> | <code>mid</code> | <code>high</code>. When set, seeker skips the compaction prompt (e.g. for PM2). <code>high</code> = most compression.</td></tr>
        <tr><td><code>MAX_TOKENS</code></td><td>4096</td><td>Max tokens per response</td></tr>
        <tr><td><code>IMAGE_EVERY_N_CYCLES</code></td><td>0</td><td>Force image every N cycles (0 = AI decides)</td></tr>
        <tr><td><code>DEBUG</code></td><td>—</td><td>Set <code>DEBUG=1</code> to log response time after each phase</td></tr>
        <tr><td><code>CLAUDE_CLI_MODEL</code></td><td>—</td><td>For <code>claude-cli</code>: model to use (<code>opus</code>, <code>sonnet</code>, <code>haiku</code>, or full id)</td></tr>
        <tr><td><code>CLAUDE_CLI_PATH</code></td><td><code>claude</code></td><td>Path to Claude CLI binary (e.g. <code>claude-dev</code>)</td></tr>
      </tbody>
    </table>
  </div>

  <div class="section">
    <div class="section-title">File structure</div>
    <pre><code>data/
  state.json          # Persistent brain state
  journal/            # Markdown diary entries (with image embeds)
  images/             # Generated images (PNG)
  image-decisions/    # Every image yes/no decision with motivation
  ledger/             # Every prompt & response (for replay)
  god/                # God mode directives</code></pre>
  </div>

  <div class="section">
    <div class="section-title">Run in the browser with PM2</div>
    <p style="color:var(--text2);font-size:15px;line-height:1.65;margin-bottom:12px">
      To see the site in a browser, you need both the <strong style="color:var(--text)">Seeker</strong> (backend) and the <strong style="color:var(--text)">web server</strong> running. PM2 runs them together and keeps them up. You need <strong style="color:var(--text)">PM2</strong> installed first:
    </p>
    <pre><code>npm install -g pm2</code></pre>
    <p style="color:var(--text2);font-size:15px;line-height:1.65;margin:12px 0">
      Then start both apps from the project folder:
    </p>
    <pre><code>pm2 start ecosystem.config.cjs
# or: yarn pm2:start

# Start and stream logs in the terminal:
yarn pm2:dev</code></pre>
    <p style="color:var(--text3);font-size:13px;margin-top:8px">
      Use the full filename <code>ecosystem.config.cjs</code> &mdash; PM2 looks for <code>ecosystem.config.js</code> by default and will error if you run plain <code>pm2 start</code>.
    </p>
    <p style="color:var(--text2);font-size:15px;line-height:1.65;margin-top:12px;margin-bottom:12px">
      Open the URL shown by the web server (e.g. <code>http://localhost:3000</code>) in your browser. To change which <strong style="color:var(--text)">models</strong> (and other run options) PM2 uses, edit <code>ecosystem.config.cjs</code>: the <code>args</code> array for the <code>meaning-seeker</code> app is <code>[thinking-model, image-model]</code>, e.g. <code>["claude-haiku", "none"]</code>. The same file sets <code>env.CYCLE_DELAY_SECONDS</code> and <code>env.COMPACTION_LEVEL</code> so the Seeker runs without interactive prompts (standard: 300s, high compression). <code>yarn pm2:dev</code> overrides to rapid (30s) with high compression and streams logs. Restart after editing: <code>pm2 restart meaning-seeker</code>.
    </p>
    <p style="color:var(--text2);font-size:15px;line-height:1.65">
      To keep the processes running after you close the terminal (e.g. on a VPS): <code>pm2 save</code> then <code>pm2 startup</code> (follow the command it prints).
    </p>
  </div>

  <p style="margin-top:32px">
    <a href="/" class="back-link">&larr; Back to Home</a>
  </p>

  ${siteFooter("how-to")}
</div>
`, "how-to", "Set up and run the Meaning Seeker — an autonomous AI that seeks the meaning of life using your choice of thinking and image models.");
}

function siteFooter() {
  return `
<footer class="site-footer">
  <div class="site-footer-inner">
    <div class="site-footer-bottom">
      <span>v${PKG_VERSION}</span>
      <span>Creator <a href="https://10101.dev" target="_blank">10101</a></span>
    </div>
  </div>
</footer>`;
}

function fmtTimeUTC(d) {
  if (!d) return "";
  return d.toLocaleTimeString("en-US", { timeZone: "UTC", hour: "2-digit", minute: "2-digit", hour12: false }) + " UTC";
}

function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderThesisBold(s) {
  return escapeHtml(s).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

function stripJsonBlocks(md) {
  return md.replace(/```(?:json)?\s*\n[\s\S]*?```/g, "").replace(/\n{3,}/g, "\n\n").trim();
}

function stripFirstRedundantHeading(md, phase) {
  const redundant = new RegExp(
    `^\\s*(#{1,2})\\s+.*(?:cycle|diary|phase|${["EXPLORE", "SYNTHESIZE", "CRITIQUE", "EVOLVE"].join("|")}).*$`,
    "im"
  );
  const firstLineMatch = md.match(/^([^\n]+)\n?/);
  if (firstLineMatch && redundant.test(firstLineMatch[1])) {
    return md.slice(firstLineMatch[0].length).trimStart();
  }
  return md;
}

function extractAndRemoveImagePrompt(md) {
  const re = /(\*\*IMAGE_PROMPT:\*\*|IMAGE_PROMPT:)\s*([\s\S]*?)(?=\n\s*---|\n\n\s*#|\n\n\s*\*\*[A-Z_]|\n\n\s*[A-Z][a-z]+:|\s*$)/i;
  const m = md.match(re);
  if (m) {
    const prompt = m[2].trim();
    const body = md.replace(m[0], "").replace(/\n{3,}/g, "\n\n").trim();
    return { body, imagePrompt: prompt || null };
  }
  return { body: md, imagePrompt: null };
}

function extractEntryTitle(content, bodyMd, cycle, phase) {
  const sepIdx = content.indexOf("\n---\n");
  const headerSection = sepIdx !== -1 ? content.slice(0, sepIdx) : "";
  const firstLineMatch = headerSection.match(/^#\s+(.+)$/m);
  const fileTitle = firstLineMatch ? firstLineMatch[1].trim() : null;
  const legacyPattern = /^Cycle \d+ — (EXPLORE|SYNTHESIZE|CRITIQUE|EVOLVE)$/;
  if (fileTitle && !legacyPattern.test(fileTitle)) return fileTitle;
  const firstH2 = bodyMd.match(/^##\s+([^\n]+)$/m);
  if (firstH2) {
    const h = firstH2[1].trim();
    if (!/cycle|diary|phase|EXPLORE|SYNTHESIZE|CRITIQUE|EVOLVE/i.test(h)) return h;
  }
  const skipPara = (p) => {
    if (p.length <= 20) return true;
    if (/^\s*[-*]\s+/.test(p)) return true;
    if (/^\s*(\*\*)?(THESIS|CONFIDENCE|IMAGE_PROMPT|INSIGHT|PARADIGM_SHIFT|SURVIVES|Model|Time|Cycle|Phase):/i.test(p)) return true;
    if (/^\s*\*\*[^*]+\*\*\s*:?\s*$/.test(p.trim())) return true;
    if (/^\s*\*\*[^*]+:\s*/.test(p)) return true;
    return false;
  };
  const bodyForFirstPara = bodyMd
    .replace(/^#+\s+[^\n]+\n?/gm, "")
    .split(/\n\n+/)
    .map((p) => p.replace(/\n/g, " ").trim())
    .find((p) => !skipPara(p));
  if (bodyForFirstPara) {
    const short = bodyForFirstPara.slice(0, 72).trim();
    const lastSpace = short.lastIndexOf(" ");
    return (lastSpace > 40 ? short.slice(0, lastSpace) : short) + (bodyForFirstPara.length > 72 ? "…" : "");
  }
  return `Cycle ${cycle} — ${phase}`;
}

function truncateTitle(s, maxLen = 36) {
  if (!s || !s.trim()) return "";
  const t = s.trim();
  return t.length <= maxLen ? t : t.slice(0, maxLen - 1).trim() + "…";
}

function journalEntryPage(filename, content, prevEntry, nextEntry) {
  const m = filename.match(/^(\d{4})-([A-Z]+)-(.+)\.md$/);
  const cycle = m ? parseInt(m[1], 10) : "?";
  const phase = m ? m[2] : "?";
  const date = m ? parseFilenameTs(m[3]) : null;
  const phaseAttr = phase && ["EXPLORE","SYNTHESIZE","CRITIQUE","EVOLVE"].includes(phase) ? ` data-phase="${phase}"` : "";

  const modelMatch = content.match(/^\*\*Model:\*\*\s*(.+)$/m);
  const model = modelMatch ? modelMatch[1].trim() : "unknown";

  const sepIdx = content.indexOf("\n---\n");
  let bodyMd = sepIdx !== -1 ? content.slice(sepIdx + 5).trimStart() : content;

  bodyMd = stripJsonBlocks(bodyMd);
  const { body: bodyNoPrompt, imagePrompt } = extractAndRemoveImagePrompt(bodyMd);
  bodyMd = stripFirstRedundantHeading(bodyNoPrompt, phase);

  const displayTitle = extractEntryTitle(content, bodyMd, cycle, phase);
  const hasImageInBody = bodyMd.includes("![");

  const prevSnippet = prevEntry
    ? truncateTitle(prevEntry.title || prevEntry.excerpt || `Cycle ${prevEntry.cycle} — ${prevEntry.phase}`, 120)
    : "";
  const nextSnippet = nextEntry
    ? truncateTitle(nextEntry.title || nextEntry.excerpt || `Cycle ${nextEntry.cycle} — ${nextEntry.phase}`, 120)
    : "";
  const splitBadgePrev = (c, ph) => {
    const pa = ph && ["EXPLORE","SYNTHESIZE","CRITIQUE","EVOLVE"].includes(ph) ? ` data-phase="${ph}"` : "";
    return `<span class="nav-split-badge"><span class="split-cycle">C${c}</span><span class="split-phase"${pa}>${ph}</span></span>`;
  };
  const splitBadgeNext = (c, ph) => {
    const pa = ph && ["EXPLORE","SYNTHESIZE","CRITIQUE","EVOLVE"].includes(ph) ? ` data-phase="${ph}"` : "";
    return `<span class="nav-split-badge"><span class="split-phase"${pa}>${ph}</span><span class="split-cycle">C${c}</span></span>`;
  };

  const prevLink = prevEntry
    ? `<a href="/journal/${encodeURIComponent(prevEntry.file)}" class="nav-arrow nav-arrow--prev"><span class="nav-arrow-label">‹ Previous</span><span class="nav-arrow-top">${splitBadgePrev(prevEntry.cycle, prevEntry.phase)}</span><span class="nav-arrow-title">${escapeHtml(prevSnippet)}</span></a>`
    : `<span class="nav-arrow nav-arrow--prev nav-arrow--empty"><span class="nav-arrow-label">‹ Previous</span></span>`;
  const nextLink = nextEntry
    ? `<a href="/journal/${encodeURIComponent(nextEntry.file)}" class="nav-arrow nav-arrow--next"><span class="nav-arrow-label">Next ›</span><span class="nav-arrow-top">${splitBadgeNext(nextEntry.cycle, nextEntry.phase)}</span><span class="nav-arrow-title">${escapeHtml(nextSnippet)}</span></a>`
    : `<span class="nav-arrow nav-arrow--next nav-arrow--empty"><span class="nav-arrow-label">Next ›</span></span>`;
  const allLink = '<a href="/journal" class="nav-all">← All entries</a>';

  const dateIso = date?.toISOString() ?? "";
  const timeMeta = date
    ? `<span class="entry-time" data-utc="${dateIso}">${fmtTimeUTC(date)}</span>`
    : "";

  return shell(displayTitle, `
<div class="container" style="padding-top:48px">
  <a href="/journal" class="back-link">&larr; Journal</a>

  <div class="entry-header">
    <div class="entry-meta-row">
      <span class="badge"${phaseAttr}>${phase}</span>
      <span>Cycle ${cycle}</span>
      <span>&middot;</span>
      <span>${model}</span>
      <span>&middot;</span>
      <span>${fmtDateLong(date)}</span>
      ${timeMeta ? `<span>&middot;</span> ${timeMeta}` : ""}
    </div>
    <script>void function(){var el=document.querySelector('.entry-time');if(el){var d=new Date(el.getAttribute('data-utc'));el.innerHTML=el.textContent+' · '+d.toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit',hour12:true});} }();</script>
    <h1 class="entry-title">${escapeHtml(displayTitle)}</h1>
  </div>

  <div class="markdown-body" id="md-body">
    <p style="color:var(--text2);font-style:italic">Rendering&hellip;</p>
  </div>
  <div class="nav-pair">
    <div class="nav-pair-row">
      ${prevLink}
      ${nextLink}
    </div>
    ${allLink}
  </div>
</div>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script>
(function(){
  var raw = ${JSON.stringify(bodyMd)};
  marked.use({ breaks: true });
  var html = marked.parse(raw);
  document.getElementById('md-body').innerHTML = html;
})();
</script>
`, "journal", "Read this journal entry from the Meaning Seeker\u2019s philosophical exploration.");
}

// ── Router ────────────────────────────────────────────────

function serve(req, res) {
  const url = new URL(req.url, "http://localhost");
  const p = url.pathname;

  // Meta image (from assets/)
  if (p === "/assets/TheMeaningSeeker.jpg") {
    const filePath = path.join(__dirname, "assets", "TheMeaningSeeker.jpg");
    if (fs.existsSync(filePath)) {
      res.writeHead(200, { "Content-Type": "image/jpeg", "Cache-Control": "public, max-age=86400" });
      fs.createReadStream(filePath).pipe(res);
      return;
    }
  }

  // Favicon & manifest (from assets/icons/)
  const assetPaths = {
    "/favicon.svg": "favicon.svg",
    "/favicon-96x96.png": "favicon-96x96.png",
    "/favicon.ico": "favicon.ico",
    "/apple-touch-icon.png": "apple-touch-icon.png",
    "/site.webmanifest": "site.webmanifest",
  };
  if (assetPaths[p]) {
    const filePath = path.join(ASSETS_ICONS_DIR, assetPaths[p]);
    if (fs.existsSync(filePath)) {
      const ext = path.extname(assetPaths[p]).toLowerCase();
      const contentType = ext === ".webmanifest" || ext === ".json" ? "application/manifest+json" : MIME[ext] ?? "application/octet-stream";
      res.writeHead(200, { "Content-Type": contentType, "Cache-Control": "public, max-age=86400" });
      fs.createReadStream(filePath).pipe(res);
      return;
    }
  }

  // Images
  if (p.startsWith("/images/")) {
    const imgFile = path.basename(p);
    const imgPath = path.join(IMAGES_DIR, imgFile);
    if (!fs.existsSync(imgPath)) {
      res.writeHead(404);
      res.end("Not found");
      return;
    }
    const ext = path.extname(imgFile).toLowerCase();
    res.writeHead(200, { "Content-Type": MIME[ext] ?? "application/octet-stream" });
    fs.createReadStream(imgPath).pipe(res);
    return;
  }

  // API: state
  if (p === "/api/state") {
    const state = readState();
    res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-cache" });
    res.end(JSON.stringify(state));
    return;
  }

  // API: journals
  if (p === "/api/journals") {
    const journals = listJournals();
    res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-cache" });
    res.end(JSON.stringify(journals));
    return;
  }

  // Journal entry
  if (p.startsWith("/journal/")) {
    const filename = decodeURIComponent(p.slice("/journal/".length));
    const content = readJournalFile(filename);
    if (!content) {
      res.writeHead(404);
      res.end("Not found");
      return;
    }
    const journals = listJournals();
    const idx = journals.findIndex((j) => j.file === path.basename(filename));
    const prevEntry = idx < journals.length - 1 ? journals[idx + 1] : null; // older
    const nextEntry = idx > 0 ? journals[idx - 1] : null; // newer
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end(journalEntryPage(path.basename(filename), content, prevEntry, nextEntry));
    return;
  }

  // Journal list
  if (p === "/journal") {
    const journals = listJournals();
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end(journalListPage(journals));
    return;
  }

  // Insights
  if (p === "/insights") {
    const state = readState();
    const journals = listJournals();
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end(insightsPage(state || {}, journals));
    return;
  }

  // How to run
  if (p === "/how-to") {
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end(howToPage());
    return;
  }

  // Art Gallery
  if (p === "/gallery") {
    const items = listGalleryItems();
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end(galleryPage(items));
    return;
  }

  // Home
  if (p === "/" || p === "") {
    const state = readState();
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end(homePage(state));
    return;
  }

  res.writeHead(404, { "Content-Type": "text/plain" });
  res.end("Not found");
}

// ── Start ─────────────────────────────────────────────────

const server = http.createServer(serve);
server.listen(PORT, () => {
  console.log(`🔮 Meaning Seeker web UI`);
  console.log(`   http://localhost:${PORT}`);
  console.log(`   Press Ctrl+C to stop`);
});
