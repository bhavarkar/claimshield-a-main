"""Case file storage: uploads and generated reports. Local directory or a Cloud Storage bucket."""

from __future__ import annotations

from pathlib import Path
import os
import tempfile
import threading


class StorageConcurrencyError(Exception):
    """Raised when an optimistic concurrency check (generation or version match) fails."""
    pass


class LocalStorage:
    name = "local filesystem"

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        """Resolve an object key beneath the storage root, including through symlinks."""
        if not isinstance(key, str) or not key or "\x00" in key:
            raise ValueError("Storage key must be a non-empty relative path")
        path = (self.root / key).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Storage key must remain inside the storage root") from exc
        return path

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        with self._lock:
            return self._put(key, data)

    def _put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.writing-')
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return f"file://{path}"

    def create(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        with self._lock:
            path = self._path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(data)

    def get(self, key: str) -> bytes:
        with self._lock:
            path = self._path(key)
            if not path.exists():
                raise FileNotFoundError(f"No such file: {key}")
            return path.read_bytes()

    def exists(self, key: str) -> bool:
        with self._lock:
            return self._path(key).exists()

    def get_with_generation(self, key: str) -> tuple[bytes, int]:
        with self._lock:
            path = self._path(key)
            if not path.exists():
                raise FileNotFoundError(f"No such file: {key}")
            stat = path.stat()
            gen = int(stat.st_mtime_ns) ^ int(stat.st_size)
            return path.read_bytes(), gen if gen != 0 else 1

    def put_if_generation_match(self, key: str, data: bytes, generation: int, content_type: str = "application/octet-stream") -> str:
        with self._lock:
            path = self._path(key)
            if generation == 0:
                if path.exists():
                    raise StorageConcurrencyError(f"Object {key} already exists (expected generation 0)")
            else:
                if not path.exists():
                    raise StorageConcurrencyError(f"Object {key} does not exist (expected generation {generation})")
                stat = path.stat()
                current_gen = int(stat.st_mtime_ns) ^ int(stat.st_size)
                if current_gen == 0:
                    current_gen = 1
                if current_gen != generation:
                    raise StorageConcurrencyError(f"Generation mismatch for {key}: current {current_gen} != expected {generation}")
            return self._put(key, data)


class GcsStorage:
    name = "Cloud Storage"

    def __init__(self, project: str, bucket: str) -> None:
        from google.cloud import storage

        self._bucket = storage.Client(project=project).bucket(bucket)
        self._name = bucket

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        self._bucket.blob(key).upload_from_string(data, content_type=content_type)
        return f"gs://{self._name}/{key}"

    def create(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._bucket.blob(key).upload_from_string(data, content_type=content_type, if_generation_match=0)

    def get(self, key: str) -> bytes:
        from google.cloud.exceptions import NotFound
        try:
            return self._bucket.blob(key).download_as_bytes()
        except NotFound as exc:
            raise FileNotFoundError(f"Blob {key} not found") from exc

    def exists(self, key: str) -> bool:
        return self._bucket.blob(key).exists()

    def get_with_generation(self, key: str) -> tuple[bytes, int]:
        from google.cloud.exceptions import NotFound
        try:
            blob = self._bucket.get_blob(key)
        except NotFound as exc:
            raise FileNotFoundError(f"Blob {key} not found") from exc
        if blob is None:
            raise FileNotFoundError(f"Blob {key} not found")
        data = blob.download_as_bytes()
        return data, int(blob.generation or 1)

    def put_if_generation_match(self, key: str, data: bytes, generation: int, content_type: str = "application/octet-stream") -> str:
        from google.cloud.exceptions import PreconditionFailed
        blob = self._bucket.blob(key)
        try:
            blob.upload_from_string(data, content_type=content_type, if_generation_match=generation)
        except PreconditionFailed as exc:
            raise StorageConcurrencyError(f"GCS generation mismatch for {key}: expected {generation}") from exc
        return f"gs://{self._name}/{key}"
