"""The metadata block a DAV serializer appends to DESCRIPTION — taken back
off what the phone sends (pure: no Firestore, no Flask).

``hearing_to_vevent`` appends « Dossier:/Type:/Modalité:/… » lines to a
hearing's notes, ``task_to_vtodo`` a « Dossier: … » line to a task's
description: display for the phone, never part of the record. The phone
sends the text back as it was served, so each model strips its own block
before storing (``models.hearing`` / ``models.task``
``strip_dav_description_suffix``).

Until the finitions (sync-7) only the block that was the exact TAIL of the
incoming text was removed. Two ordinary phone gestures defeated that: the
lawyer typing at the END of the description — where the phone shows the
block, so the natural place — which leaves the block in the middle; and a
calendar app moving an event between collections, which re-uploads it
under a new name (the create branch, where nothing was stripped at all). The
block then landed in the stored text for good, and was served again with a
fresh block after it.

:func:`remove_served_block` removes the block wherever it stands as a run
of WHOLE lines — the serializer's exact output, never a line the lawyer
retouched (a single changed character keeps the whole run). Linear: one
pass over the lines, no regex.
"""

from __future__ import annotations


def remove_served_block(text: str, block: str, *, blank_line_before: bool) -> str:
    """*text* without every whole-line occurrence of *block*.

    *block* is the serializer's output, its lines joined by ``"\\n"``.
    *blank_line_before*: the serializer separates the block from the text by
    a blank line (``"\\n\\n"`` — tasks); the blank line that preceded a
    removed block goes with it. CRLF lines compare equal to their LF form,
    and the ``"\\r"`` a removed block leaves at the end of the text (the
    CRLF join) goes too. Everything else is returned byte for byte.
    """
    if not block or not isinstance(text, str) or not text:
        return text
    want = block.split("\n")
    size = len(want)
    raw = text.split("\n")
    cmp = [line[:-1] if line.endswith("\r") else line for line in raw]
    kept_raw: list[str] = []
    kept_cmp: list[str] = []
    changed = False
    index = 0
    while index < len(raw):
        if cmp[index:index + size] == want:
            if blank_line_before and kept_cmp and kept_cmp[-1] == "":
                kept_raw.pop()
                kept_cmp.pop()
            index += size
            changed = True
            continue
        kept_raw.append(raw[index])
        kept_cmp.append(cmp[index])
        index += 1
    if not changed:
        return text
    result = "\n".join(kept_raw)
    if result.endswith("\r") and not raw[-1].endswith("\r"):
        result = result[:-1]
    return result
