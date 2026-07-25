"""Quarantine-bucket storage for the portal (spec L1 §4, §7).

The portal's ONLY write surface besides the task queue: object CREATION in
the quarantine bucket (IAM: ``storage.objectCreator`` — no read, no list,
no overwrite). File bytes never transit the portal service — the browser
PUTs chunks straight to the GCS resumable-session URL.
"""

import json
import unicodedata
from datetime import datetime, timezone
from functools import lru_cache

from google.cloud import storage

from client.config import PORTAIL_BUCKET, PORTAIL_HOST

_NOM_MAX = 180


@lru_cache(maxsize=1)
def _bucket() -> storage.Bucket:
    return storage.Client().bucket(PORTAIL_BUCKET)


def assainir_nom(nom: str) -> str:
    """Sanitize a client-supplied file name for use as an object-name segment.

    NFC normalization, path separators → ``_``, control characters removed,
    no leading/trailing dots, ≤ 180 chars (extension preserved), fallback
    ``document``. The FULL original name is preserved in the envelope
    (valeur probante) — this sanitized form only names the object.
    """
    nom = unicodedata.normalize("NFC", str(nom or ""))
    nom = nom.replace("\\", "_").replace("/", "_")
    nom = "".join(c for c in nom if unicodedata.category(c)[0] != "C")
    nom = nom.strip().strip(".")
    if len(nom) > _NOM_MAX:
        stem, dot, ext = nom.rpartition(".")
        if dot and 0 < len(ext) <= 10:
            nom = stem[: _NOM_MAX - len(ext) - 1] + "." + ext
        else:
            nom = nom[:_NOM_MAX]
    return nom or "document"


def horodatage_utc_compact() -> str:
    """Batch identifier: compact UTC timestamp (e.g. ``20260725T143059``)."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def ouvrir_session_reprenable(objet: str, content_type: str, size: int) -> str:
    """Open a GCS resumable-upload session and return its URL.

    Two properties are load-bearing (spec §7.2):
    - ``origin`` sets the CORS policy OF THE SESSION itself — no bucket-level
      CORS configuration is needed for the browser's chunk PUTs.
    - ``size`` becomes ``X-Upload-Content-Length`` — GCS ITSELF refuses any
      upload larger than declared, so the per-file cap holds even against a
      hostile client.
    """
    blob = _bucket().blob(objet)
    return blob.create_resumable_upload_session(
        content_type=content_type,
        size=size,
        origin=f"https://{PORTAIL_HOST}",
    )


def ecrire_enveloppe(inv_id: str, batch: str, envelope: dict) -> None:
    """Write ``envelope.json`` create-only (``if_generation_match=0``).

    A second finalization of the same batch raises PreconditionFailed (412),
    which the route maps to 409 « Transmission déjà soumise ». The envelope
    is the DURABLE TRUTH of a submission — reconciliation replays anything
    whose envelope exists (spec §8.4).
    """
    blob = _bucket().blob(f"submissions/{inv_id}/{batch}/envelope.json")
    blob.upload_from_string(
        json.dumps(envelope, ensure_ascii=False, default=str),
        content_type="application/json",
        if_generation_match=0,
    )
