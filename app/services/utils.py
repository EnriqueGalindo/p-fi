# app/services/utils.py
import json
import time
import logging
from typing import Any, Optional, Tuple
from google.cloud import storage
from google.api_core.exceptions import NotFound, Forbidden

# very small in-process TTL cache
_cache: dict[Tuple[str, str], tuple[float, Any]] = {}

def get_json_from_gcs(
    bucket: str,
    path: str,
    default: Any = None,
    *,
    ttl: int = 0,
    client: Optional[storage.Client] = None,
) -> Any:
    """
    Read a JSON object from GCS: gs://<bucket>/<path>
    - Returns `default` if the object doesn't exist or can't be parsed.
    - Optional TTL (seconds) to cache reads in-process.
    - You may pass an existing google.cloud.storage.Client via `client`.
    """
    key = (bucket, path)
    now = time.time()

    if ttl and key in _cache:
        exp, val = _cache[key]
        if now < exp:
            return val

    if client is None:
        client = storage.Client()

    try:
        blob = client.bucket(bucket).blob(path)
        text = blob.download_as_text()  # utf-8
        val = json.loads(text)
    except NotFound:
        return default
    except Forbidden:
        logging.exception("Forbidden reading gs://%s/%s", bucket, path)
        raise
    except Exception:
        logging.exception("Failed reading/parsing gs://%s/%s", bucket, path)
        return default

    if ttl:
        _cache[key] = (now + ttl, val)
    return val
