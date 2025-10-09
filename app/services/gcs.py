import json
from typing import List, Optional
from google.cloud import storage

class GcsStore:
    def __init__(self, bucket_name: str):
        self.client = storage.Client()
        self.bucket = self.client.bucket(bucket_name)

    def read_text(self, path: str) -> Optional[str]:
        blob = self.bucket.blob(path)
        if not blob.exists():
            return None
        return blob.download_as_text()

    def write_text(self, path: str, text: str, content_type: str = "text/plain") -> None:
        blob = self.bucket.blob(path)
        blob.upload_from_string(text, content_type=content_type)

    def read_json(self, path: str):
        t = self.read_text(path)
        return None if t is None else json.loads(t)

    def write_json(self, path: str, obj) -> None:
        self.write_text(path, json.dumps(obj, ensure_ascii=False, indent=2), "application/json")

    def list_paths(self, prefix: str) -> List[str]:
        return [b.name for b in self.client.list_blobs(self.bucket, prefix=prefix)]

