# Meaning Seeker — Data Compaction & Archival Addendum

## Problem

Even with three-tier memory, the archive layer and images grow without bound. At 288 cycles/day (5-minute intervals), the system produces ~5GB/month, mostly from images and the Prompt Ledger. A cheap VPS fills up in a few months.

## Growth Estimates (5-minute cycle interval)

| Data type | Per cycle | Per day | Per month | Per year |
|-----------|-----------|---------|-----------|----------|
| Journal (4 .md files) | ~20KB | 5.6MB | 168MB | ~2GB |
| Prompt Ledger (4 .json) | ~70KB | 19.7MB | 590MB | ~7GB |
| Crystal snapshots | ~1KB | 0.3MB | 9MB | ~108MB |
| Images (every 3 cycles) | ~500KB avg | 140MB | 4.2GB | ~50GB |
| Rejected claims growth | negligible | negligible | negligible | ~1MB |
| **Total** | ~91KB+ | **~166MB** | **~5GB** | **~60GB** |

Images dominate. The Prompt Ledger is second. Everything else is manageable.

---

## Compaction Strategy

### 1. Prompt Ledger — Compress After 24 Hours

The Ledger is critical for replayability but rarely re-read. Compress daily.

```javascript
// compactor.mjs — run via cron or at end of each day
import fs from 'fs';
import path from 'path';
import zlib from 'zlib';

const LEDGER_DIR = 'data/ledger';
const LEDGER_ARCHIVE = 'data/archive/ledger';

export function compactLedger(maxAgeHours = 24) {
  const cutoff = Date.now() - (maxAgeHours * 60 * 60 * 1000);
  const files = fs.readdirSync(LEDGER_DIR).filter(f => f.endsWith('.json'));

  // Group old files by day
  const byDay = {};
  for (const file of files) {
    const filepath = path.join(LEDGER_DIR, file);
    const stat = fs.statSync(filepath);
    if (stat.mtimeMs < cutoff) {
      const day = new Date(stat.mtime).toISOString().slice(0, 10); // YYYY-MM-DD
      if (!byDay[day]) byDay[day] = [];
      byDay[day].push(filepath);
    }
  }

  // Compress each day into a single .jsonl.gz
  for (const [day, filepaths] of Object.entries(byDay)) {
    const entries = filepaths.map(fp => fs.readFileSync(fp, 'utf-8'));
    const jsonl = entries.join('\n');
    const compressed = zlib.gzipSync(jsonl);

    fs.mkdirSync(LEDGER_ARCHIVE, { recursive: true });
    fs.writeFileSync(path.join(LEDGER_ARCHIVE, `ledger-${day}.jsonl.gz`), compressed);

    // Remove originals
    filepaths.forEach(fp => fs.unlinkSync(fp));
    console.log(`Compacted ${filepaths.length} ledger entries for ${day} (${(compressed.length / 1024).toFixed(1)}KB)`);
  }
}
```

**Compression ratio for JSON text: ~85-90%.** So 590MB/month → ~70MB/month compressed.

### 2. Journal — Compact Into Weekly Digests

Individual journal entries are human-readable but redundant once the crystal manuscript captures the insights. Compact weekly.

```javascript
export function compactJournal(maxAgeDays = 7) {
  const JOURNAL_DIR = 'data/archive/journal';
  const DIGEST_DIR = 'data/archive/digests';
  const cutoff = Date.now() - (maxAgeDays * 24 * 60 * 60 * 1000);

  const files = fs.readdirSync(JOURNAL_DIR)
    .filter(f => f.endsWith('.md'))
    .map(f => ({ name: f, path: path.join(JOURNAL_DIR, f), mtime: fs.statSync(path.join(JOURNAL_DIR, f)).mtimeMs }))
    .filter(f => f.mtime < cutoff);

  if (files.length === 0) return;

  // Group by ISO week
  const byWeek = {};
  for (const file of files) {
    const date = new Date(file.mtime);
    const week = getISOWeek(date);
    const key = `${date.getFullYear()}-W${String(week).padStart(2, '0')}`;
    if (!byWeek[key]) byWeek[key] = [];
    byWeek[key].push(file);
  }

  fs.mkdirSync(DIGEST_DIR, { recursive: true });

  for (const [week, weekFiles] of Object.entries(byWeek)) {
    // Concatenate all entries into a single markdown digest
    const contents = weekFiles
      .sort((a, b) => a.mtime - b.mtime)
      .map(f => fs.readFileSync(f.path, 'utf-8'))
      .join('\n\n---\n\n');

    // Compress the digest
    const compressed = zlib.gzipSync(contents);
    fs.writeFileSync(path.join(DIGEST_DIR, `journal-${week}.md.gz`), compressed);

    // Remove individual files
    weekFiles.forEach(f => fs.unlinkSync(f.path));
    console.log(`Compacted ${weekFiles.length} journal entries for ${week} (${(compressed.length / 1024).toFixed(1)}KB)`);
  }
}

function getISOWeek(date) {
  const d = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
  d.setUTCDate(d.getUTCDate() + 4 - (d.getUTCDay() || 7));
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  return Math.ceil((((d - yearStart) / 86400000) + 1) / 7);
}
```

**Result:** 168MB/month → ~20MB/month compressed, and only ~3MB/month after a few weeks because the digests compress much better than individual files.

### 3. Images — The Big One

Images are the largest cost. Three strategies, pick based on your priorities:

#### Option A: Reduce Frequency (Simplest)

Change image generation from every 3 cycles to every 10-15 cycles. At every 10 cycles:
- ~29 images/day × ~2MB = ~58MB/day = ~1.7GB/month

```javascript
// In seeker.mjs or config
const IMAGE_EVERY_N_CYCLES = 10; // was 3
```

#### Option B: Compress and Resize

Keep frequency but compress after generation. Most AI images are much larger than needed for a diary.

```javascript
import { execSync } from 'child_process';

export function compressImage(inputPath) {
  // Requires: apt install imagemagick
  // Resize to max 1024px wide, convert to JPEG at 80% quality
  const outputPath = inputPath.replace(/\.png$/, '.jpg');
  execSync(`convert "${inputPath}" -resize 1024x1024\\> -quality 80 "${outputPath}"`);
  fs.unlinkSync(inputPath); // Remove original PNG
  return outputPath;
}
```

**Compression ratio:** PNG (~2-3MB) → JPEG 80% resized (~150-300KB). That's a **10x reduction**.
- At every 3 cycles: 4.2GB/month → ~420MB/month
- At every 10 cycles: 1.7GB/month → ~170MB/month

#### Option C: Rolling Image Archive (Recommended)

Keep only the last N images accessible. Move older images to compressed archives.

```javascript
export function archiveOldImages(keepRecent = 100) {
  const IMAGE_DIR = 'data/images';
  const IMAGE_ARCHIVE = 'data/archive/images';

  const files = fs.readdirSync(IMAGE_DIR)
    .filter(f => /\.(jpg|jpeg|png|webp)$/i.test(f))
    .map(f => ({ name: f, path: path.join(IMAGE_DIR, f), mtime: fs.statSync(path.join(IMAGE_DIR, f)).mtimeMs }))
    .sort((a, b) => b.mtime - a.mtime); // newest first

  if (files.length <= keepRecent) return;

  const toArchive = files.slice(keepRecent);
  fs.mkdirSync(IMAGE_ARCHIVE, { recursive: true });

  // Group by month and tar.gz
  const byMonth = {};
  for (const file of toArchive) {
    const month = new Date(file.mtime).toISOString().slice(0, 7); // YYYY-MM
    if (!byMonth[month]) byMonth[month] = [];
    byMonth[month].push(file);
  }

  for (const [month, monthFiles] of Object.entries(byMonth)) {
    const archivePath = path.join(IMAGE_ARCHIVE, `images-${month}.tar.gz`);
    const filePaths = monthFiles.map(f => f.path).join(' ');

    // Use tar to compress (append if archive exists, create if not)
    if (fs.existsSync(archivePath)) {
      // Extract existing, add new files, recompress
      const tempDir = `/tmp/img-archive-${month}`;
      execSync(`mkdir -p ${tempDir} && tar xzf "${archivePath}" -C ${tempDir} 2>/dev/null; cp ${filePaths} ${tempDir}/; cd ${tempDir} && tar czf "${archivePath}" *.{jpg,jpeg,png,webp} 2>/dev/null; rm -rf ${tempDir}`);
    } else {
      execSync(`tar czf "${archivePath}" ${filePaths}`);
    }

    // Remove archived originals
    monthFiles.forEach(f => fs.unlinkSync(f.path));
    console.log(`Archived ${monthFiles.length} images for ${month}`);
  }
}
```

### 4. Crystal Snapshots — Already Small, But Cap Them

```javascript
export function pruneSnapshots(keepRecent = 30) {
  const SNAPSHOT_DIR = 'data/archive/snapshots';
  if (!fs.existsSync(SNAPSHOT_DIR)) return;

  const files = fs.readdirSync(SNAPSHOT_DIR)
    .filter(f => f.startsWith('crystal-v'))
    .sort()
    .reverse(); // newest first

  if (files.length <= keepRecent) return;

  // Keep first, last, and every 10th — delete the rest
  const toKeep = new Set();
  toKeep.add(files[0]); // newest
  toKeep.add(files[files.length - 1]); // oldest

  for (let i = 0; i < files.length; i += 10) {
    toKeep.add(files[i]);
  }

  // Keep the most recent `keepRecent` regardless
  files.slice(0, keepRecent).forEach(f => toKeep.add(f));

  for (const file of files) {
    if (!toKeep.has(file)) {
      fs.unlinkSync(path.join(SNAPSHOT_DIR, file));
    }
  }
}
```

### 5. Rejected Claims — Cap the List

In the memory redesign doc, `rejected_claims` in `crystal.json` grows forever. Cap it:

```javascript
// In CrystalMemory.save() or during manuscript rewrite
if (this.data.rejected_claims.length > 50) {
  // Keep 50 most recent — old rejected claims are unlikely to resurface
  this.data.rejected_claims = this.data.rejected_claims.slice(-50);
}
```

---

## Automated Compaction Schedule

Add a compaction step to the main loop, running once per day (every 288 cycles at 5-min intervals):

```javascript
// In seeker.mjs main loop
const COMPACTION_INTERVAL = 288; // cycles (= ~24 hours at 5-min intervals)

async function maybeCompact(cycle) {
  if (cycle % COMPACTION_INTERVAL !== 0 || cycle === 0) return;

  console.log('🗜️  Running daily compaction...');

  compactLedger(24);          // Compress ledger entries older than 24h
  compactJournal(7);          // Digest journal entries older than 7 days
  archiveOldImages(100);      // Keep only last 100 images live
  pruneSnapshots(30);         // Keep max 30 crystal snapshots

  // Log disk usage
  const usage = execSync('du -sh data/ data/archive/ 2>/dev/null').toString().trim();
  console.log(`📊 Disk usage:\n${usage}`);
}
```

Or alternatively, set up a cron job:

```bash
# /etc/cron.d/meaning-seeker-compaction
0 3 * * * cd /path/to/meaning-seeker && node -e "import('./compactor.mjs').then(m => m.runAll())"
```

---

## Revised Growth Estimates (With Compaction)

| Data type | Raw/month | After compaction | Notes |
|-----------|-----------|-----------------|-------|
| Prompt Ledger | 590MB | ~70MB | gzip compression |
| Journal | 168MB | ~20MB | weekly digests, gzipped |
| Crystal snapshots | 9MB | ~5MB | pruned to 30 + milestones |
| Images (Option B+C) | 4.2GB | ~400MB | resized + rolling archive |
| Working/crystal JSON | negligible | negligible | capped, rewritten |
| **Total** | **~5GB** | **~500MB** | **~90% reduction** |

**At 500MB/month, a 25GB VPS lasts ~4 years.** Plenty of runway.

If you want even leaner, increase image interval to 10 cycles → ~170MB/month for images → total ~265MB/month → **8+ years** on 25GB.

---

## Files to Create/Modify

| File | Action |
|------|--------|
| `compactor.mjs` | **Create** — all compaction functions (ledger, journal, images, snapshots) |
| `seeker.mjs` | **Modify** — add `maybeCompact()` call in main loop |
| `crystal.json` handling | **Modify** — cap rejected_claims at 50 |
| `package.json` | **Modify** — ensure no new dependencies needed (uses Node built-ins + imagemagick on system) |
| VPS setup | **Note** — need `apt install imagemagick` for image compression |

---

## Disk Space Monitoring

Add a safety check that pauses the seeker if disk gets low:

```javascript
import { execSync } from 'child_process';

function checkDiskSpace(minFreeGB = 2) {
  try {
    const output = execSync("df -BG --output=avail / | tail -1").toString().trim();
    const freeGB = parseInt(output);
    if (freeGB < minFreeGB) {
      console.error(`⚠️ LOW DISK: ${freeGB}GB free (minimum: ${minFreeGB}GB). Pausing seeker.`);
      console.error('Run compaction manually or increase disk space.');
      return false;
    }
    return true;
  } catch {
    return true; // Don't block on check failure
  }
}

// Call before each cycle
if (!checkDiskSpace()) {
  // Force compaction, then check again
  await runFullCompaction();
  if (!checkDiskSpace()) {
    process.exit(1); // Still low — need human intervention
  }
}
```
