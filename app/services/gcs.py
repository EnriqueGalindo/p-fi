import json, time
from typing import List, Optional
from google.cloud import storage
from google.api_core.exceptions import TooManyRequests
from google.api_core.retry import Retry

from google.api_core import exceptions as gax_exc

# Prefer the storage client's default retry if present
try:
    from google.cloud.storage.retry import DEFAULT_RETRY as GCS_DEFAULT_RETRY
except Exception:  # older libs
    GCS_DEFAULT_RETRY = None

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

        # Try library-level retry first (newer google-cloud-storage)
        if GCS_DEFAULT_RETRY is not None:
            try:
                blob.upload_from_string(text, content_type=content_type, retry=GCS_DEFAULT_RETRY, timeout=60)
                return
            except TypeError:
                # Some versions don’t accept retry kwarg on this call
                pass
            except (gax_exc.TooManyRequests, gax_exc.ServiceUnavailable, gax_exc.DeadlineExceeded):
                # Fall through to manual backoff below
                pass

        # Manual exponential backoff for transient errors
        backoff = 0.5
        for attempt in range(6):  # ~0.5 + 1 + 2 + 4 + 8 + 8 ~= 23.5s
            try:
                blob.upload_from_string(text, content_type=content_type, timeout=60)
                return
            except (gax_exc.TooManyRequests, gax_exc.ServiceUnavailable, gax_exc.DeadlineExceeded):
                if attempt == 5:
                    raise
                time.sleep(backoff)
                backoff = min(backoff * 2, 8.0)

    def read_json(self, path: str):
        t = self.read_text(path)
        return None if t is None else json.loads(t)

    def write_json(self, path, obj):
        # Prefer real delete if obj is None
        if obj is None and hasattr(self, "delete"):
            try:
                return self.delete(path)
            except Exception:
                pass

        text = "null" if obj is None else json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        self.write_text(path, text, "application/json")

    def list_paths(self, prefix: str) -> List[str]:
        return [b.name for b in self.client.list_blobs(self.bucket, prefix=prefix)]
    
    def delete(self, path):
        blob = self.bucket.blob(path)
        try:
            blob.delete()
        except gax_exc.NotFound:
            pass


# # app/services/gcs.py

# class GCSStore:
#     def __init__(self, bucket):
#         self.bucket = bucket

#     def write_text(self, path, text, content_type="text/plain"):
#         blob = self.bucket.blob(path)

#         # Try library-level retry first (newer google-cloud-storage)
#         if GCS_DEFAULT_RETRY is not None:
#             try:
#                 blob.upload_from_string(text, content_type=content_type, retry=GCS_DEFAULT_RETRY, timeout=60)
#                 return
#             except TypeError:
#                 # Some versions don’t accept retry kwarg on this call
#                 pass
#             except (gax_exc.TooManyRequests, gax_exc.ServiceUnavailable, gax_exc.DeadlineExceeded):
#                 # Fall through to manual backoff below
#                 pass

#         # Manual exponential backoff for transient errors
#         backoff = 0.5
#         for attempt in range(6):  # ~0.5 + 1 + 2 + 4 + 8 + 8 ~= 23.5s
#             try:
#                 blob.upload_from_string(text, content_type=content_type, timeout=60)
#                 return
#             except (gax_exc.TooManyRequests, gax_exc.ServiceUnavailable, gax_exc.DeadlineExceeded):
#                 if attempt == 5:
#                     raise
#                 time.sleep(backoff)
#                 backoff = min(backoff * 2, 8.0)

#     def write_json(self, path, obj):
#         # Prefer real delete if obj is None
#         if obj is None and hasattr(self, "delete"):
#             try:
#                 return self.delete(path)
#             except Exception:
#                 pass

#         text = "null" if obj is None else json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
#         self.write_text(path, text, "application/json")

#     # Optional convenience delete if you don’t already have one:
#     def delete(self, path):
#         blob = self.bucket.blob(path)
#         try:
#             blob.delete()
#         except gax_exc.NotFound:
#             pass
