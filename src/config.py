import re
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))


def _parse_daily_at_utc(value: str) -> tuple[int, int] | None:
    """Parse HH:MM or H:MM (UTC). Returns (hour, minute) or None if invalid."""
    if not value or not (s := value.strip()):
        return None
    m = re.match(r"^(?P<h>\d{1,2}):(?P<m>\d{2})$", s)
    if not m:
        return None
    try:
        h, m_val = int(m.group("h")), int(m.group("m"))
        if 0 <= h <= 23 and 0 <= m_val <= 59:
            return (h, m_val)
    except ValueError:
        pass
    return None

# API: OpenRouter (primary — used with Claude) or direct OpenAI. Same OpenAI-compatible client.
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")

# Use OpenAI directly if OPENAI_API_KEY is set; otherwise use OpenRouter.
if OPENAI_API_KEY:
    API_KEY: str = OPENAI_API_KEY
    API_BASE_URL: str | None = None  # OpenAI default
    API_PROVIDER: str = "openai"
else:
    API_KEY = OPENROUTER_API_KEY
    API_BASE_URL = OPENROUTER_BASE_URL
    API_PROVIDER = "openrouter"

MODEL_ID: str = os.getenv("MODEL_ID", "anthropic/claude-sonnet-4")
SITE_URL: str = os.getenv("SITE_URL", "https://meaning-seeker.com")
SITE_NAME: str = os.getenv("SITE_NAME", "Meaning Seeker")

CYCLE_INTERVAL_SECONDS: int = int(os.getenv("CYCLE_INTERVAL_SECONDS", "3600"))
_cycle_daily_raw: str = os.getenv("CYCLE_DAILY_AT_UTC", "").strip()
CYCLE_DAILY_AT_UTC_PARSED: tuple[int, int] | None = (
    _parse_daily_at_utc(_cycle_daily_raw) if _cycle_daily_raw else None
)

# API call: timeout (seconds) and retries for transient failures
API_TIMEOUT_SECONDS: int = int(os.getenv("API_TIMEOUT_SECONDS", "180"))
API_RETRY_ATTEMPTS: int = 2  # initial try + 2 retries = 3 total
API_RETRY_BACKOFF_SECONDS: tuple[int, ...] = (10, 30)

MAX_ACTIVE_TENSIONS: int = 15
TRANSITION_LOG_TOKEN_BUDGET: int = 2000
TENSION_TOKEN_BUDGET: int = 1500

# Phase B: Monitoring (lightweight model for repetition, substance, summary, threat mapping)
MONITOR_MODEL_ID: str = os.getenv("MONITOR_MODEL_ID", "anthropic/claude-3.5-haiku")

# Phase B: Archive retrieval (Ollama + Chroma)
OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_EMBED_MODEL: str = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
CHROMA_DIR: Path = DATA_DIR / "chroma"
ARCHIVE_RETRIEVAL_COUNT: int = 3
ARCHIVE_SNIPPET_TOKEN_LIMIT: int = 500

# Phase B: Silence mechanism
SILENCE_DURATION: int = 3
SILENCE_COOLDOWN: int = 10

# Phase B: Pattern Monitor
MOVE_LOG_SIZE: int = 20
PATTERN_THRESHOLD_SAME_MOVE: int = 5  # same move in 5 of last 10 cycles
PATTERN_THRESHOLD_NO_GROUNDING: int = 6  # no "concrete grounding" for 12+ cycles

# Phase C: Web server (127.0.0.1 when behind Caddy; 0.0.0.0 for direct access)
WEB_HOST: str = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT: int = int(os.getenv("WEB_PORT", "3000"))

# Phase C: CSRF protection for submission form (set in production)
CSRF_SECRET: str = os.getenv("CSRF_SECRET", "dev-only-change-in-production")

# Phase C: Submission rate limiting
SUBMISSION_RATE_LIMIT: str = os.getenv("SUBMISSION_RATE_LIMIT", "3/hour")
SUBMISSION_MIN_LENGTH: int = 50
SUBMISSION_MAX_LENGTH: int = 2000

# Phase C: Filtering pipeline (reuse monitor model)
FILTER_MODEL_ID: str = os.getenv("FILTER_MODEL_ID", os.getenv("MONITOR_MODEL_ID", "anthropic/claude-3.5-haiku"))

# Phase C: Social media platforms
X_API_KEY: str = os.getenv("X_API_KEY", "")
X_API_SECRET: str = os.getenv("X_API_SECRET", "")
X_ACCESS_TOKEN: str = os.getenv("X_ACCESS_TOKEN", "")
X_ACCESS_SECRET: str = os.getenv("X_ACCESS_SECRET", "")
X_BEARER_TOKEN: str = os.getenv("X_BEARER_TOKEN", "")
BLUESKY_HANDLE: str = os.getenv("BLUESKY_HANDLE", "")
BLUESKY_PASSWORD: str = os.getenv("BLUESKY_PASSWORD", "")
THREADS_ACCESS_TOKEN: str = os.getenv("THREADS_ACCESS_TOKEN", "")
THREADS_USER_ID: str = os.getenv("THREADS_USER_ID", "")
INSTAGRAM_ACCESS_TOKEN: str = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
INSTAGRAM_USER_ID: str = os.getenv("INSTAGRAM_USER_ID", "")

# Phase C: Social media profile URLs (for "follow" links on challenge page)
X_PROFILE_URL: str = os.getenv("X_PROFILE_URL", "")
BLUESKY_PROFILE_URL: str = os.getenv("BLUESKY_PROFILE_URL", "")
THREADS_PROFILE_URL: str = os.getenv("THREADS_PROFILE_URL", "")
INSTAGRAM_PROFILE_URL: str = os.getenv("INSTAGRAM_PROFILE_URL", "")

# Phase C: Image generation via OpenRouter (uses same API_KEY as main model)
# Nano Banana Pro (Gemini 3 Pro Image Preview) — superior style fidelity and creative control
IMAGE_MODEL: str = os.getenv("IMAGE_MODEL", "google/gemini-3-pro-image-preview")
# Force image this cycle (for testing). When set, generate an image even if the model didn't ask for one; prompt from summary or thesis.
FORCE_IMAGE: bool = os.getenv("FORCE_IMAGE", "").lower() in ("1", "true", "yes")

# Phase C: Social moderation (local-first, Haiku only for edge cases)
SOCIAL_MODERATION_ENABLED: bool = os.getenv("SOCIAL_MODERATION_ENABLED", "true").lower() == "true"

# Challenge System v2: weekly review and organic weaving
MAX_REVIEW_CHALLENGES: int = int(os.getenv("MAX_REVIEW_CHALLENGES", "10"))
ORGANIC_WEAVE_THRESHOLD: float = float(os.getenv("ORGANIC_WEAVE_THRESHOLD", "0.3"))
# Force Sunday weekly review this cycle (for testing). When set, run weekly review instead of normal injection.
FORCE_WEEKLY_REVIEW: bool = os.getenv("FORCE_WEEKLY_REVIEW", "").lower() in ("1", "true", "yes")
