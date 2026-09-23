import os

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from logging_config import logger


def load_limiter_storage_uri(env=os.environ):
    """Where Flask-Limiter keeps its rate-limit counters. With no REDIS_URL it falls back to an
    in-process dict ("memory://"): safe with a single worker, but each worker then has its own
    counters, so N workers behind one process manager give N times the intended limit (and a
    counter reset on every restart) rather than a real per-client limit. Set REDIS_URL to share
    counters across workers/containers -- required for the login rate limit to mean what it says
    once you run more than one worker."""
    url = env.get("REDIS_URL", "").strip()
    if url:
        return url
    if env.get("FLASK_ENV") == "production":
        logger.warning("REDIS_URL is not set: rate limiting uses in-memory storage, so limits are "
                       "per-worker (not shared) and reset on restart. Set REDIS_URL before running "
                       "more than one worker.")
    return "memory://"


limiter = Limiter(key_func=get_remote_address, default_limits=[], storage_uri=load_limiter_storage_uri())
