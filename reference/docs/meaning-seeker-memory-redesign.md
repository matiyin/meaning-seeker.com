# Meaning Seeker — Memory Architecture Redesign

## Context for Cursor

You are modifying **Meaning Seeker**, an autonomous philosophical AI that runs continuously on a VPS, cycling through four phases: EXPLORE → SYNTHESIZE → CRITIQUE → EVOLVE. The system currently accumulates all insights in a growing `data/memory.json` file and writes sequential markdown journal entries. **This must change.**

The core insight driving this redesign: the AI has a **fixed dataset** (its model training). It cannot gain new experiences. Unlike a human who gets fresh input from the world, this system can only extract depth from what it already knows. Therefore the memory system must be optimized for **depth extraction, not breadth accumulation.** Forgetting is a feature, not a bug.

---

## Current Architecture (what exists now)

```
data/
├── memory.json          # Growing blob: thesis, insights[], questions[], contradictions[]
├── evolution.json       # Append-only timeline of thesis changes
├── journal/             # One markdown file per iteration, never pruned
└── images/              # AI-generated philosophical illustrations
```

**Problems:**
1. `memory.json` grows indefinitely — insights array capped at 200 but still bloats context
2. All insights are weighted equally — no distinction between profound and routine
3. No forgetting mechanism — the system re-encounters its own stale ideas every cycle
4. Journal is write-only — never consulted during reasoning
5. The `summary_for_prompt()` function dumps the last 15 insights regardless of relevance
6. No adversarial pressure on the CRITIQUE phase
7. Domain rotation is linear, not driven by tension or unresolved questions

---

## New Architecture: Three-Tier Memory

Replace the current flat `memory.json` with three distinct memory tiers:

### Tier 1: Working Memory (`data/working.json`)

**Purpose:** Small, volatile, high-pressure. Only what the current cycle needs.

```json
{
  "current_thesis": "string",
  "confidence": 0.0,
  "active_tensions": [
    {
      "id": "t-001",
      "tension": "Consciousness seems both necessary for meaning and irreducible to physical processes",
      "domains": ["consciousness", "ethics"],
      "created_cycle": 42,
      "challenge_count": 0
    }
  ],
  "current_phase": "explore",
  "current_domain": "string",
  "last_critique_result": {
    "weakest_assumption": "string",
    "attack_vector": "string",
    "survived": true
  },
  "forced_collision": {
    "domain_a": "thermodynamics",
    "domain_b": "justice",
    "prompt": "What does entropy reveal about fairness?"
  }
}
```

**Rules:**
- Maximum 5 active tensions at any time
- Tensions that survive 3 critique cycles get promoted to Tier 2
- Tensions that get resolved are discarded (not archived)
- `forced_collision` is regenerated every EXPLORE phase — pick two random domains that haven't been paired before
- `last_critique_result` is overwritten every cycle, not appended

### Tier 2: Crystallized Insights (`data/crystal.json`)

**Purpose:** The system's evolving manuscript. Gets **rewritten**, not appended.

```json
{
  "manuscript_version": 17,
  "last_rewritten_cycle": 156,
  "core_claims": [
    {
      "id": "c-001",
      "claim": "Meaning emerges from the tension between pattern and entropy",
      "supporting_evidence": ["Brief summary of argument 1", "Brief summary of argument 2"],
      "strongest_objection": "This may conflate physical processes with phenomenal experience",
      "confidence": 0.7,
      "domain_roots": ["information theory", "consciousness"],
      "first_articulated_cycle": 23,
      "last_challenged_cycle": 150
    }
  ],
  "rejected_claims": [
    {
      "id": "c-008",
      "claim": "Meaning requires an observer",
      "rejection_reason": "Undermined by analysis of mathematical beauty — patterns seem meaningful independent of observation",
      "rejected_at_cycle": 89
    }
  ],
  "thesis_lineage": [
    {
      "thesis": "string",
      "held_from_cycle": 1,
      "held_until_cycle": 45,
      "reason_abandoned": "string"
    }
  ],
  "domain_collision_log": {
    "thermodynamics+justice": { "explored_cycle": 67, "yielded_insight": true },
    "consciousness+mathematics": { "explored_cycle": 71, "yielded_insight": false }
  }
}
```

**Rules:**
- Maximum 15 core claims at any time
- Every 10 cycles, trigger a **manuscript rewrite**: the EVOLVE phase rewrites `crystal.json` from scratch based on current understanding (not by appending)
- Claims that haven't been challenged in 20 cycles get flagged for mandatory re-examination
- `rejected_claims` is kept permanently — this prevents the system from re-discovering and re-adopting ideas it already rejected
- `domain_collision_log` tracks which domain pairs have been explored so the system doesn't repeat combinations

### Tier 3: Archive (`data/archive/`)

**Purpose:** Cold storage that the system CANNOT access during active reasoning. For human review only.

```
data/archive/
├── journal/              # Moved here from data/journal/
├── evolution.json        # Historical thesis timeline
├── snapshots/
│   ├── crystal-v001.json # Snapshot of crystal.json before each rewrite
│   ├── crystal-v002.json
│   └── ...
└── metrics.json          # Aggregate stats for monitoring
```

**Rules:**
- Journal entries move here after being written (the system never reads them back)
- Crystal snapshots are saved before each rewrite so humans can track evolution
- `metrics.json` tracks novelty scores, cycle counts, domain coverage — for external dashboards only
- **The system prompt must NOT reference or load anything from `data/archive/`**

---

## Changes to Phase Logic

### EXPLORE Phase — Add Forced Collisions

Modify the EXPLORE phase to alternate between two modes:

**Mode A: Domain Deep Dive** (current behavior, keep it)

**Mode B: Cross-Domain Collision** (new, alternate with Mode A)

When in collision mode:
1. Pick two domains that haven't been paired (check `domain_collision_log`)
2. If all pairs exhausted, pick the pair that was explored longest ago
3. Frame the prompt as: "What does [Domain A] reveal about [Domain B] that neither domain can see on its own?"
4. Store whether it yielded a genuine insight

```javascript
// In the phase selection logic:
function getExploreMode(cycle, crystal) {
  if (cycle % 2 === 0) return 'deep_dive';
  return 'collision';
}

function pickCollisionPair(crystal, domains) {
  const explored = crystal.domain_collision_log || {};
  const allPairs = [];
  for (let i = 0; i < domains.length; i++) {
    for (let j = i + 1; j < domains.length; j++) {
      const key = [domains[i], domains[j]].sort().join('+');
      allPairs.push({ key, a: domains[i], b: domains[j], explored: explored[key] });
    }
  }
  // Prefer unexplored pairs, then oldest explored
  const unexplored = allPairs.filter(p => !p.explored);
  if (unexplored.length > 0) {
    return unexplored[Math.floor(Math.random() * unexplored.length)];
  }
  allPairs.sort((a, b) => a.explored.explored_cycle - b.explored.explored_cycle);
  return allPairs[0];
}
```

### CRITIQUE Phase — Make it Adversarial

The current CRITIQUE phase asks the model to self-critique. This is too gentle. Change it:

1. **Identify the weakest assumption** in the current thesis (ask the model explicitly)
2. **Construct the strongest possible counter-argument** against that assumption
3. **Ask the model to defend** the thesis against that specific attack
4. **Record whether the thesis survived** in `working.json`

If the thesis fails the critique:
- Demote the relevant core claim in `crystal.json` (reduce confidence)
- If confidence drops below 0.3, move to `rejected_claims`
- Force the EVOLVE phase to address the failure

Add this to the CRITIQUE system prompt:

```
Your task is not to gently examine your thesis. Your task is to BREAK it.

1. Identify the single weakest assumption in the current thesis.
2. Construct the most devastating counter-argument a hostile philosopher could make.
3. Attempt to defend your thesis against this attack.
4. Be honest: did the thesis survive? If not, what must change?

You are not allowed to say "the thesis remains strong." If it actually is strong, explain precisely WHY the counter-argument fails. Vague defenses count as failures.
```

### EVOLVE Phase — Add Constraint Pressure

Every 5th EVOLVE cycle, impose an **artificial constraint**:

```javascript
const constraints = [
  "Reformulate your thesis using only concepts a 10-year-old could understand.",
  "Express your thesis in exactly one sentence, no semicolons.",
  "Derive your thesis from only 3 premises. State them explicitly.",
  "Your thesis must account for the existence of suffering without minimizing it.",
  "Explain your thesis to someone who believes life is meaningless. What would you say?",
  "Remove the most abstract word from your thesis and reformulate without it.",
  "Your thesis must be falsifiable. State what evidence would disprove it.",
];

function getEvolveConstraint(cycle) {
  if (cycle % 5 !== 0) return null;
  return constraints[Math.floor(cycle / 5) % constraints.length];
}
```

### SYNTHESIZE Phase — Manuscript Rewrite Trigger

Every 10 cycles, the SYNTHESIZE phase triggers a **full manuscript rewrite** instead of normal synthesis:

```javascript
function shouldRewriteManuscript(cycle, crystal) {
  return cycle % 10 === 0 && cycle > 0;
}
```

During a manuscript rewrite:
1. Load `crystal.json`
2. Save a snapshot to `data/archive/snapshots/crystal-v{N}.json`
3. Ask the model to rewrite all core claims from scratch, keeping only what it can re-derive
4. Claims it cannot re-derive get moved to `rejected_claims`
5. This is the primary **forgetting mechanism** — ideas that can't survive re-derivation are dropped

Prompt for manuscript rewrite:
```
You are rewriting your philosophical manuscript from scratch.

Below are your current core claims. For each one:
- Can you re-derive it from your current understanding? If yes, keep it (possibly refined).
- If you cannot justify it anymore, reject it with a reason.
- Add any new claims that have emerged from recent cycles.

Maximum 15 core claims. Each must have a clear argument, a strongest objection, and a confidence score.

Do not keep claims out of attachment. Only keep what you can defend right now.
```

---

## Changes to the System Prompt

Replace the current `summary_for_prompt()` function. The new version should build context from Tier 1 and Tier 2 only, never from the archive:

```javascript
function buildContextForPrompt(working, crystal) {
  const sections = [];

  // Current thesis and confidence
  sections.push(`CURRENT THESIS (confidence: ${working.confidence}):\n"${working.current_thesis}"`);

  // Active tensions (max 5)
  if (working.active_tensions.length > 0) {
    const tensions = working.active_tensions
      .map(t => `- [${t.domains.join(' × ')}] ${t.tension} (challenged ${t.challenge_count}x)`)
      .join('\n');
    sections.push(`UNRESOLVED TENSIONS:\n${tensions}`);
  }

  // Last critique result
  if (working.last_critique_result) {
    const cr = working.last_critique_result;
    sections.push(`LAST CRITIQUE:\nWeakest assumption: ${cr.weakest_assumption}\nAttack: ${cr.attack_vector}\nSurvived: ${cr.survived}`);
  }

  // Core claims from manuscript (max 15)
  if (crystal.core_claims.length > 0) {
    const claims = crystal.core_claims
      .map(c => `- [${c.confidence.toFixed(1)}] ${c.claim}\n  Objection: ${c.strongest_objection}`)
      .join('\n');
    sections.push(`CORE CLAIMS:\n${claims}`);
  }

  // Recently rejected claims (last 5 only — to prevent re-adoption)
  const recentRejections = crystal.rejected_claims.slice(-5);
  if (recentRejections.length > 0) {
    const rejections = recentRejections
      .map(r => `- REJECTED: "${r.claim}" — ${r.rejection_reason}`)
      .join('\n');
    sections.push(`DEAD ENDS (do not revisit):\n${rejections}`);
  }

  // Forced collision if applicable
  if (working.forced_collision) {
    const fc = working.forced_collision;
    sections.push(`COLLISION PROMPT: ${fc.prompt}`);
  }

  return sections.join('\n\n');
}
```

---

## Changes to the Response JSON Schema

Update the expected response format from the model:

```json
{
  "phase_reflection": "Deep philosophical exploration (2000+ words)",
  "key_insight": "Single most important insight (1-3 sentences)",
  "insight_domain": "Primary domain",
  "insight_significance": "low | medium | high | paradigm_shift",
  "updated_thesis": "Current thesis (can be unchanged)",
  "thesis_changed": true,
  "thesis_change_reasoning": "Why it changed",
  "confidence_score": 0.65,
  "new_tensions": ["Tensions discovered this cycle"],
  "resolved_tensions": ["Tension IDs that were resolved"],
  "weakest_assumption": "The most vulnerable part of the current thesis",
  "attack_on_weakest": "The strongest counter-argument against it",
  "defense_result": "survived | wounded | failed",
  "new_core_claim": {
    "claim": "If a new claim emerged worth crystallizing",
    "supporting_evidence": ["argument 1", "argument 2"],
    "strongest_objection": "Best counter-argument"
  },
  "meta_observation": "Observation about own thinking process"
}
```

**Removed:** `new_questions`, `new_contradictions`, `is_paradigm_shift`, `paradigm_shift_description`, `next_domain_suggestion`

**Replaced with:** `new_tensions`, `resolved_tensions`, `insight_significance`, `weakest_assumption`, `attack_on_weakest`, `defense_result`, `new_core_claim`

---

## Migration Plan

### Step 1: Create new file structure

Create the new files alongside the old ones:

```
data/
├── working.json         # NEW — Tier 1
├── crystal.json         # NEW — Tier 2
├── archive/             # NEW — Tier 3
│   ├── journal/         # Move existing journal/ contents here
│   ├── snapshots/
│   └── metrics.json
├── memory.json          # OLD — keep for migration, then delete
├── evolution.json       # OLD — move to archive/
└── images/              # Keep as-is
```

### Step 2: Write a migration script

Create `migrate.mjs` that:
1. Reads existing `memory.json`
2. Extracts `current_thesis` and `confidence_score` → `working.json`
3. Takes the last 5 contradictions → `working.json` as `active_tensions`
4. Takes the top 15 insights (by domain diversity) → `crystal.json` as `core_claims`
5. Moves `journal/` contents to `archive/journal/`
6. Moves `evolution.json` to `archive/evolution.json`
7. Backs up `memory.json` to `archive/memory-backup.json`

### Step 3: Update `memory.mjs`

Replace the current `Memory` class and `summary_for_prompt()` with:
- `WorkingMemory` class (loads/saves `working.json`)
- `CrystalMemory` class (loads/saves `crystal.json`)
- `buildContextForPrompt(working, crystal)` function as specified above
- Journal write function that saves to `archive/journal/` directly

### Step 4: Update `seeker.mjs`

- Import new memory classes
- Modify phase logic to use new explore modes, adversarial critique, constraint pressure
- Add manuscript rewrite trigger every 10 cycles
- Update response parsing to handle new JSON schema

### Step 5: Update the system prompt

- Remove references to "open questions" and "contradictions" (replaced by tensions)
- Add the adversarial critique instructions
- Add collision mode instructions
- Add constraint instructions for EVOLVE
- Update JSON response format

---

## Novelty Detection (Optional but Recommended)

Add a simple similarity check to detect when the system is producing repetitive insights:

```javascript
import crypto from 'crypto';

function computeInsightFingerprint(insight) {
  // Simple: hash the first 100 chars normalized
  const normalized = insight.toLowerCase().replace(/[^a-z ]/g, '').trim().slice(0, 100);
  return crypto.createHash('md5').update(normalized).digest('hex').slice(0, 8);
}

function isNovel(newInsight, recentFingerprints) {
  const fp = computeInsightFingerprint(newInsight);
  if (recentFingerprints.includes(fp)) return false;
  recentFingerprints.push(fp);
  if (recentFingerprints.length > 50) recentFingerprints.shift();
  return true;
}
```

When a non-novel insight is detected, force a collision mode on the next EXPLORE cycle and log a warning. Three consecutive non-novel cycles should trigger an automatic God Mode alert.

---

## Summary of All File Changes

| File | Action | Description |
|------|--------|-------------|
| `memory.mjs` | **Rewrite** | Replace `Memory` class with `WorkingMemory` + `CrystalMemory`. New `buildContextForPrompt()`. Journal writes to archive. |
| `seeker.mjs` | **Modify** | New phase logic (collision explore, adversarial critique, constraint evolve, manuscript rewrite). New response schema parsing. |
| `migrate.mjs` | **Create** | One-time migration from old memory.json to new three-tier structure. |
| `data/working.json` | **Create** | Tier 1 working memory (auto-created on first run). |
| `data/crystal.json` | **Create** | Tier 2 crystallized insights (auto-created on first run or via migration). |
| `data/archive/` | **Create** | Tier 3 cold storage directory structure. |
| `god.mjs` | **Modify** | Update `status` command to read from new memory files. Update `steer` to inject tensions. |
| `dashboard.py` | **Modify** | Update to read `working.json` and `crystal.json` instead of `memory.json`. |

---

## Key Principles to Maintain

1. **Forgetting is a feature.** The system gets smarter by dropping weak ideas, not by accumulating more.
2. **Tension over agreement.** The working memory should always contain unresolved contradictions. A system with zero tensions is stagnating.
3. **Rewrite over append.** The crystal manuscript gets rewritten, not grown. If a claim can't survive re-derivation, it deserves to die.
4. **Constraint breeds depth.** The artificial constraints on EVOLVE force the system to distill rather than elaborate.
5. **Collision breeds novelty.** Cross-domain collisions are the closest thing to "new experience" in a closed system.
