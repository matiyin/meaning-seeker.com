import json
import logging

from .config import API_PROVIDER, DATA_DIR, MODEL_ID

logger = logging.getLogger(__name__)


def save_prompt_record(
    cycle: int,
    system_prompt: str,
    user_message: str,
    raw_response: str,
    prompt_hash: str,
) -> None:
    record = {
        "cycle": cycle,
        "model_id": MODEL_ID,
        "provider": API_PROVIDER,
        "prompt_hash": prompt_hash,
        "system_prompt": system_prompt,
        "user_message": user_message,
        "raw_response": raw_response,
    }
    path = DATA_DIR / "prompts" / f"{cycle:06d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.debug(f"Cycle {cycle}: prompt record saved")
