import json, time
from typing import List, Optional
from google.cloud import storage
from google.api_core.exceptions import TooManyRequests
from google.api_core.retry import Retry

TRANSIENT_RETRY = Retry(
    predicate=Retry.if_transient_error,
    initial=0.5, maximum=8.0, multiplier=2.0, deadline=30.0
)

class GcsStore:
    def __init__(self, bucket_name: str):
        self.client = storage.Client()
        self.bucket = self.client.bucket(bucket_name)

    def read_text(self, path: str) -> Optional[str]:
        blob = self.bucket.blob(path)
        if not blob.exists():
            return None
        return blob.download_as_text()

    def write_text(self, path, text, content_type="text/plain"):
        blob = self.bucket.blob(path)
        # Try library-level retry first
        try:
            blob.upload_from_string(text, content_type=content_type, retry=TRANSIENT_RETRY, timeout=60)
            return
        except TypeError:
            # older libs may not support 'retry' param on this call; fall through
            pass

        # Fallback: manual backoff for 429
        backoff = 0.5
        for attempt in range(6):
            try:
                blob.upload_from_string(text, content_type=content_type, timeout=60)
                return
            except TooManyRequests:
                if attempt == 5:
                    raise
                time.sleep(backoff)
                backoff = min(backoff * 2, 8.0)

    def read_json(self, path: str):
        t = self.read_text(path)
        return None if t is None else json.loads(t)

    def write_json(self, path, obj):
        # compact ‘None’ delete-compat; avoid huge payloads often
        if obj is None:
            # prefer real delete if possible
            if hasattr(self, "delete"):
                try:
                    return self.delete(path)
                except Exception:
                    pass
            text = "null"
        else:
            text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        self.write_text(path, text, "application/json")

    def list_paths(self, prefix: str) -> List[str]:
        return [b.name for b in self.client.list_blobs(self.bucket, prefix=prefix)]

