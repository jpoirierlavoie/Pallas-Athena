"""The Secret Manager check never prints a secret's VALUE — proven, not promised.

`utils.config_checks.check_prod_secrets` reads every secret's payload (it has
to: an empty value, a stray newline or a wrong shape are the failures it
exists to catch) and reports on it — to the CLI (`scripts/check_config.py`),
to « Paramètres → Configuration » (the rows), and as JSON. Its comments say
it prints « only the shape »; CodeQL's py/clear-text-logging-sensitive-data
flags the CLI printer on the word « secret » in the names it prints. This
module turns the comment into a gate: every payload carries a marker, every
branch the check can take is driven, and the marker must appear in none of
the printed lines, the report rows (message AND detail — what the page
renders), `render_text` or `render_json`.

A fake `google.cloud.secretmanager` stands in for the service; no credential,
no network.
"""

import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import config_checks  # noqa: E402
from utils.deployment_inventory import SECRETS  # noqa: E402
from utils.deployment_report import Report, render_json, render_text  # noqa: E402

MARK = "Zq7mark"   # token-safe AND bcrypt-alphabet, so it survives every shape


class NotFound(Exception):
    """Named like the service's own error — the check reads the class name."""


class PermissionDenied(Exception):
    pass


def _fill(length: int) -> str:
    return (MARK * (length // len(MARK) + 1))[:length]


def _valid(spec) -> str:
    """A payload the spec's shape accepts, marker included."""
    if spec.shape is None:
        return MARK
    if spec.shape.minimum == spec.shape.maximum:          # bcrypt, 60 chars
        return "$2b$12$" + _fill(spec.shape.maximum - 7)
    return _fill(spec.shape.minimum)


def _unusual(spec) -> str:
    """Within the bounds, ASCII, but outside the token alphabet → WARN."""
    base = _valid(spec)
    return base[:-1] + "+"


# Each variant maps a spec to what the fake service answers for it: bytes (a
# payload) or an exception instance. Exception texts carry the marker too —
# the check must print the class name, never the message.
_VARIANTS = {
    "ok": lambda spec: _valid(spec).encode(),
    "stray_whitespace": lambda spec: (_valid(spec) + "\n").encode(),
    "bad_shape": lambda spec: (MARK * 60).encode(),
    "non_ascii": lambda spec: (_valid(spec)[:-1] + "é").encode(),
    "unusual_shape": lambda spec: _unusual(spec).encode(),
    "empty": lambda spec: b"",
    "absent": lambda spec: NotFound(f"secret {MARK} not found"),
    "unreadable": lambda spec: PermissionDenied(f"denied on {MARK}"),
}


class _Client:
    def __init__(self, variant: str) -> None:
        self._variant = variant
        self._by_id = {s.secret_id: s for s in SECRETS}

    def access_secret_version(self, request):
        secret_id = request["name"].split("/secrets/")[1].split("/")[0]
        answer = _VARIANTS[self._variant](self._by_id[secret_id])
        if isinstance(answer, Exception):
            raise answer
        return types.SimpleNamespace(
            payload=types.SimpleNamespace(data=answer),
            name=f"projects/p/secrets/{secret_id}/versions/7",
        )


@pytest.fixture()
def fake_secret_manager(monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "test-project")

    def _install(variant: str) -> None:
        module = types.ModuleType("google.cloud.secretmanager")
        module.SecretManagerServiceClient = lambda: _Client(variant)
        monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", module)
        import google.cloud

        monkeypatch.setattr(google.cloud, "secretmanager", module, raising=False)

    return _install


def _run(install, variant: str):
    install(variant)
    printed: list[str] = []
    rpt = Report(printer=printed.append)
    config_checks.check_prod_secrets(rpt)
    return rpt, printed


def _every_output(rpt, printed) -> str:
    rows = "\n".join(f"{r.message} {r.detail!r}" for r in rpt.rows)
    return "\n".join([
        "\n".join(printed), rows, render_text(rpt),
        json.dumps(render_json(rpt), ensure_ascii=False),
    ])


@pytest.mark.parametrize("variant", sorted(_VARIANTS))
def test_no_branch_prints_the_payload(fake_secret_manager, variant):
    rpt, printed = _run(fake_secret_manager, variant)
    # Every secret was reported on — the check did not stop early.
    assert len(rpt.rows) == len(SECRETS)
    assert MARK not in _every_output(rpt, printed)


def test_the_variants_reach_every_verdict_the_check_can_give(fake_secret_manager):
    """Without this, a variant quietly landing on the wrong branch would leave
    a branch unproven while the test above stayed green."""
    reached = set()
    for variant in _VARIANTS:
        rpt, _printed = _run(fake_secret_manager, variant)
        reached |= {r.detail.get("outcome") for r in rpt.rows}
    assert reached >= {
        "ok", "stray_whitespace", "bad_shape", "unusual_shape",
        "empty", "absent", "unreadable",
    }, reached


def test_the_marker_would_be_seen_if_a_branch_printed_it(fake_secret_manager):
    """The gate is not vacuous: a report that DID carry the payload fails."""
    rpt, printed = _run(fake_secret_manager, "ok")
    rpt.emit("WARN", f"payload was {_valid(SECRETS[0])}")
    assert MARK in _every_output(rpt, printed)
