"""A small, realistic in-memory Cloud Storage for the test suite.

Why not ``mock.MagicMock``: a MagicMock blob accepts EVERY call — a rewrite
over an existing object, a delete of another writer's generation, a patch
of a vanished object — and answers each with another MagicMock. Tests
written against it can only assert which methods were called, never what
the bucket HOLDS afterwards, and they stay green while the code destroys a
committed document's bytes. This fake keeps objects with their bytes,
generation and metadata, and REFUSES what the service refuses.

The API mimicked is ``google-cloud-storage`` 3.10.1 (the exact pin), checked
against ``inspect.signature`` of ``google.cloud.storage.blob.Blob`` on
2026-09-27:

* ``Blob.rewrite(source, token=None, client=None, if_generation_match=None,
  …)`` → ``(token, bytes_rewritten, total_bytes)``. The copy completes in
  ``rewrite_chunk`` bytes per call; until then a token is returned and
  NOTHING is visible at the destination (the service creates the object
  on the final call only). On completion the blob's properties are set
  from the new object (``_set_properties(api_response["resource"])`` in
  the library). ``if_generation_match=0`` → ``PreconditionFailed`` (412)
  when a live object exists; a missing source → ``NotFound`` (404).
* ``Blob.reload(…)`` → loads ``size``, ``md5_hash``, ``crc32c``,
  ``generation``, ``content_type``, ``content_disposition``; ``NotFound``
  on a missing object. Before a reload (or a completed write) a new
  ``Blob`` reports ``size``/``generation`` as ``None``, like the library.
* ``Blob.patch(…, if_generation_match=None, …)`` — persists
  ``content_type``/``content_disposition``; ``NotFound`` / 412.
* ``Blob.delete(…, if_generation_match=None, …)`` — ``NotFound`` / 412.
* ``Blob.download_as_bytes(start=None, end=None, …)`` — ``end`` INCLUSIVE.
* ``Blob.upload_from_file(file_obj, …, content_type=None, …,
  if_generation_match=None, …)``.
* ``Blob.upload_from_string(data, content_type='text/plain', client=None,
  predefined_acl=None, if_generation_match=None, …)`` — ``str`` data is
  UTF-8-encoded, as the library does; ``if_generation_match=0`` →
  ``PreconditionFailed`` (412) when a live object exists (create-only).
* ``Blob.time_created`` — the object's creation instant (``timeCreated``),
  a timezone-aware ``datetime``; ``None`` before a reload, like every
  other server property. :meth:`FakeBucket.put` takes ``time_created=``
  so a test can plant an object that is already old.
* ``Blob.create_resumable_upload_session(content_type=None, size=None,
  origin=None, …, if_generation_match=None, …)`` → a session URL string;
  :meth:`FakeBucket.complete_session` plays the browser's (or a sandbox's)
  PUT. The blob's WRITABLE metadata set before the call travels in the
  initiation, as in the library (``_get_writable_metadata``: ``md5Hash`` is
  one of ``_WRITABLE_FIELDS``, so a ``blob.md5_hash = …`` is sent): the
  session records it, and the final PUT is refused when its bytes do not
  hash to it — the service's own check of a declared MD5. The session's
  ``if_generation_match`` is evaluated when the object is created (a
  create-only session refuses an object that appeared meanwhile), and a
  session creates ONE object: a second PUT to a completed session is
  refused.
* ``Blob.download_as_bytes(…, if_generation_match=None, …)`` — 412 when the
  live generation differs.
* ``md5_hash``/``crc32c`` are base64 strings, as the service returns them.

Deliberately NOT modelled: soft delete, object versioning, ACLs, CMEK,
composite objects (which carry no MD5), retries.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from google.api_core.exceptions import NotFound, PreconditionFailed

__all__ = ["FakeBucket", "FakeBlob", "StoredObject"]


@dataclass
class StoredObject:
    data: bytes
    generation: int
    content_type: Optional[str] = None
    content_disposition: Optional[str] = None
    time_created: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))

    @property
    def md5_hash(self) -> str:
        # GCS's content MD5 — an integrity check, not a security hash.
        return base64.b64encode(hashlib.md5(self.data, usedforsecurity=False).digest()).decode()  # nosec B303

    @property
    def crc32c(self) -> str:
        # zlib.crc32 is CRC-32, not CRC-32C; the value only has to be a
        # stable content checksum for the code under test, which compares
        # it and never recomputes it.
        return base64.b64encode(
            zlib.crc32(self.data).to_bytes(4, "big")).decode()


@dataclass
class _Rewrite:
    source: str
    source_generation: int
    done: int


@dataclass
class FakeBucket:
    """``storage.bucket()`` stand-in: patch it in with ``lambda: bucket``."""

    name: str = "fake-bucket"
    rewrite_chunk: int = 64 * 1024 * 1024
    objects: dict[str, StoredObject] = field(default_factory=dict)
    # Called at the START of every rewrite call with (dest_name, source_name):
    # the seam a concurrent writer is slipped into.
    rewrite_hooks: list[Callable[[str, str], None]] = field(default_factory=list)
    sessions: dict[str, dict] = field(default_factory=dict)
    _generations: itertools.count = field(
        default_factory=lambda: itertools.count(1_000_001))
    _rewrites: dict[str, _Rewrite] = field(default_factory=dict)
    _tokens: itertools.count = field(default_factory=lambda: itertools.count(1))

    # ── test setup / assertions ────────────────────────────────────────

    def put(self, name: str, data: bytes, *, content_type: Optional[str] = None,
            content_disposition: Optional[str] = None,
            time_created: Optional[datetime] = None) -> StoredObject:
        """Store *data* at *name* (a new generation), as another writer would."""
        obj = StoredObject(data, next(self._generations), content_type,
                           content_disposition)
        if time_created is not None:
            obj.time_created = time_created
        self.objects[name] = obj
        return obj

    def remove(self, name: str) -> None:
        self.objects.pop(name, None)

    def complete_session(self, url: str, data: bytes) -> StoredObject:
        """Play the PUT to a resumable session URL — refused as the service
        refuses it: bytes beyond ``size=``, bytes whose MD5 is not the one
        declared at initiation, an object that appeared under a create-only
        session, a second PUT to a completed session."""
        session = self.sessions[url]
        if session.get("completed"):
            raise AssertionError("the service refuses a completed session")
        if session["size"] is not None and len(data) != session["size"]:
            raise AssertionError("the service refuses bytes beyond size=")
        declared = session.get("md5_hash")
        if declared is not None:
            # GCS's content MD5 — an integrity check, not a security hash.
            actual = base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()  # nosec B303
            if actual != declared:
                raise AssertionError(
                    "the service refuses bytes whose MD5 is not the declared one")
        self._check_generation(session["name"], session["if_generation_match"])
        session["completed"] = True
        return self.put(session["name"], data,
                        content_type=session["content_type"])

    # ── the client surface ─────────────────────────────────────────────

    def blob(self, name: str) -> "FakeBlob":
        return FakeBlob(self, name)

    def _check_generation(self, name: str, if_generation_match) -> None:
        if if_generation_match is None:
            return
        current = self.objects.get(name)
        live = current.generation if current is not None else 0
        if int(if_generation_match) != live:
            raise PreconditionFailed(f"412 conditionNotMet: {name}")


class FakeBlob:
    def __init__(self, bucket: FakeBucket, name: str):
        self.bucket = bucket
        self.name = name
        self.size: Optional[int] = None
        self.generation: Optional[int] = None
        self.md5_hash: Optional[str] = None
        self.crc32c: Optional[str] = None
        self.content_type: Optional[str] = None
        self.content_disposition: Optional[str] = None
        self.time_created: Optional[datetime] = None

    def _load(self, obj: StoredObject) -> None:
        self.size = len(obj.data)
        self.generation = obj.generation
        self.md5_hash = obj.md5_hash
        self.crc32c = obj.crc32c
        self.content_type = obj.content_type
        self.content_disposition = obj.content_disposition
        self.time_created = obj.time_created

    def _live(self) -> StoredObject:
        obj = self.bucket.objects.get(self.name)
        if obj is None:
            raise NotFound(f"404 No such object: {self.name}")
        return obj

    def reload(self, client=None, **_kwargs) -> None:
        self._load(self._live())

    def exists(self, client=None, **_kwargs) -> bool:
        return self.name in self.bucket.objects

    def download_as_bytes(self, client=None, start=None, end=None,
                          if_generation_match=None, **_kwargs) -> bytes:
        self._live()
        self.bucket._check_generation(self.name, if_generation_match)
        data = self._live().data
        begin = start or 0
        stop = len(data) if end is None else end + 1   # end is INCLUSIVE
        return data[begin:stop]

    def upload_from_file(self, file_obj, rewind=False, size=None,
                         content_type=None, client=None, predefined_acl=None,
                         if_generation_match=None, **_kwargs) -> None:
        self.bucket._check_generation(self.name, if_generation_match)
        if rewind:
            file_obj.seek(0)
        data = file_obj.read() if size is None else file_obj.read(size)
        obj = self.bucket.put(
            self.name, data, content_type=content_type or self.content_type,
            content_disposition=self.content_disposition)
        self._load(obj)

    def upload_from_string(self, data, content_type="text/plain", client=None,
                           predefined_acl=None, if_generation_match=None,
                           **_kwargs) -> None:
        self.bucket._check_generation(self.name, if_generation_match)
        if isinstance(data, str):
            data = data.encode("utf-8")
        obj = self.bucket.put(
            self.name, bytes(data), content_type=content_type,
            content_disposition=self.content_disposition)
        self._load(obj)

    def rewrite(self, source: "FakeBlob", token=None, client=None,
                if_generation_match=None, **_kwargs):
        for hook in list(self.bucket.rewrite_hooks):
            hook(self.name, source.name)
        # The precondition is evaluated on EVERY call, the continuations
        # included — GCS requires them to repeat the first call's
        # parameters, and the destination may have appeared meanwhile.
        self.bucket._check_generation(self.name, if_generation_match)
        if token is None:
            src = source.bucket.objects.get(source.name)
            if src is None:
                raise NotFound(f"404 No such object: {source.name}")
            token = f"tok-{next(self.bucket._tokens)}"
            self.bucket._rewrites[token] = _Rewrite(source.name, src.generation, 0)
        state = self.bucket._rewrites[token]
        src = source.bucket.objects.get(state.source)
        if src is None or src.generation != state.source_generation:
            raise NotFound(f"404 No such object: {state.source}")
        total = len(src.data)
        state.done = min(total, state.done + self.bucket.rewrite_chunk)
        if state.done < total:
            return token, state.done, total
        del self.bucket._rewrites[token]
        obj = self.bucket.put(self.name, src.data,
                              content_type=src.content_type,
                              content_disposition=src.content_disposition)
        self._load(obj)
        return None, total, total

    def patch(self, client=None, if_generation_match=None, **_kwargs) -> None:
        obj = self._live()
        self.bucket._check_generation(self.name, if_generation_match)
        obj.content_type = self.content_type
        obj.content_disposition = self.content_disposition
        self._load(obj)

    def delete(self, client=None, if_generation_match=None, **_kwargs) -> None:
        self._live()
        self.bucket._check_generation(self.name, if_generation_match)
        del self.bucket.objects[self.name]

    def create_resumable_upload_session(
        self, content_type=None, size=None, origin=None, client=None,
        timeout=60, checksum="auto", predefined_acl=None,
        if_generation_match=None, **_kwargs,
    ) -> str:
        url = (f"https://storage.example/upload/{self.bucket.name}/"
               f"{self.name}?upload_id={len(self.bucket.sessions) + 1}")
        self.bucket.sessions[url] = {
            "name": self.name, "content_type": content_type, "size": size,
            "origin": origin, "if_generation_match": if_generation_match,
            # The writable metadata the library sends at initiation.
            "md5_hash": self.md5_hash,
        }
        return url
