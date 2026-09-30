"""Quebec judicial deadline computation (art. 83 C.p.c.).

Two distinct families live here, and conflating them is the bug this module
exists to prevent:

* **Computation** (``compute_deadline``, ``is_juridical_day``,
  ``next_juridical_day``, ``prev_juridical_day``, ``add_jours_ouvrables``) —
  pure calendar arithmetic implementing art. 83: every day counts, and a raw
  deadline landing on a non-juridical day is pushed further in the direction
  of computation. **No clock is ever read here.** These functions are pinned
  by a frozen reference table in ``tests/test_deadlines.py``. All take a
  ``regime`` except ``compute_deadline`` (art. 83 — procedural by
  definition) and ``last_action_day`` (procedural on purpose; see its
  docstring) — see « Two calendars » below.
* **Lateness** (``today_mtl``, ``effective_due``, ``is_past_due``,
  ``days_until``) — the single answer to « is this deadline in the past? »,
  on the **Montréal** calendar, evaluated against the PROROGUED deadline
  (lawyer's decision, 2026-08-02): a due date landing on a non-juridical day
  is actionable until the next juridical day, so it is not « late » until
  the day after that. Due Saturday → actionable Monday → late Tuesday.
  Prorogation can only make lateness start LATER, never earlier — and it is
  a no-op on the computed deadlines (steps, prescription), which already
  land on juridical days by construction.

``today_mtl`` is the one place a clock is read. Every surface that needs
"today" must go through it, or two surfaces drift by up to a day: UTC runs
ahead of Montréal by 4-5 hours, so a UTC-based comparison declares a deadline
past from 20:00 (EDT) / 19:00 (EST) the evening BEFORE.

**Two calendars, not one.** Which days are « fériés » depends on the text
that governs the délai, and the two texts that matter here disagree on two
dates a year:

* ``PROCEDURAL`` — art. 82-83 C.p.c.: Saturdays, the jours fériés of art.
  61(23) L.i. (Sundays included), **plus 26 December and 2 January, which
  art. 82 makes « en matière de procédure civile, considérés jours fériés »
  EVERY year** (whatever weekday they fall on). The default of every helper:
  « jour juridique » is procedural vocabulary, and ``compute_deadline`` —
  art. 83 — is this module's reason to exist.
* ``CIVIL`` — art. 2879 C.c.Q. (prescription: « Lorsque le dernier jour est
  un samedi ou un jour férié ») and the business-day notion of « jours
  ouvrables » (Monday to Friday, excluding the art. 61(23) L.i. list —
  the definition art. 3 of the Règlement de la Cour d'appel spells out):
  Saturdays + the art. 61(23) L.i. list, and NOTHING else. Art. 82's two
  days are confined by their own words to civil procedure; the legislature
  adds them expressly where it wants them elsewhere (art. 269 L.p.c., art.
  87 of the TAL act), and neither the C.c.Q. nor the L.i. does.

Neither calendar knows a « Sunday → Monday » substitute for 1 January, 24
June or 25 December: art. 61(23) L.i. provides ONE substitute, 2 July when
1 July is a Sunday, and that is the only one here. (The earlier code added
2 January / 25 June / 26 December only when the holiday before them fell on
a Sunday — wrong for procedure whenever 26 December or 2 January is a
Tuesday to Friday, wrong for prescription on the Monday it did add, and
wrong for both calendars on 25 June.)
"""

from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional

from tz import MTL


# ── The two calendars ────────────────────────────────────────────────────

PROCEDURAL = "procedural"   # art. 82-83 C.p.c. — adds 26 Dec + 2 Jan
CIVIL = "civil"             # art. 61(23) L.i. + Saturday (art. 2879 C.c.Q.)
Regime = Literal["procedural", "civil"]
_REGIMES = (PROCEDURAL, CIVIL)

# Art. 82 C.p.c.: « non plus que les 26 décembre et 2 janvier qui sont, en
# matière de procédure civile, considérés jours fériés ». (month, day).
_ART_82_DAYS = ((12, 26), (1, 2))


def _check_regime(regime: str) -> None:
    """Refuse an unknown calendar loudly — a typo must never silently fall
    back to one of the two (each is wrong for the other's délais)."""
    if regime not in _REGIMES:
        raise ValueError(f"Calendrier inconnu : {regime!r}")


def is_art_82_day(d: date) -> bool:
    """True on 26 December and 2 January — the two days art. 82 C.p.c.
    treats as holidays in civil procedure, and ONLY there."""
    return (d.month, d.day) in _ART_82_DAYS


def compute_deadline(
    start_date: date,
    delay_days: int,
    direction: Literal["after", "before"] = "after",
) -> date:
    """Compute a judicial deadline from a start date and delay.

    Args:
        start_date: The reference date (e.g., date of service, hearing date).
        delay_days: Number of calendar days in the delay (positive integer).
        direction: "after" = deadline is start_date + delay_days (forward).
                   "before" = deadline is start_date - delay_days (backward).

    Returns:
        The adjusted deadline date. If the raw deadline falls on a
        non-juridical day, it is pushed further in the direction of
        computation until it lands on a juridical day.

    Always the PROCEDURAL calendar (art. 82 C.p.c.): this IS art. 83, so a
    deadline landing on 26 December or 2 January is extended like one
    landing on a Saturday — whatever weekday those dates fall on.

    Examples:
        # 15 days after March 1, 2025 = March 16 (Sunday) → March 17 (Monday)
        compute_deadline(date(2025, 3, 1), 15, "after")

        # 10 days before March 14, 2025 = March 4 (Tuesday) → March 4 (no change)
        compute_deadline(date(2025, 3, 14), 10, "before")
    """
    if direction == "after":
        raw = start_date + timedelta(days=delay_days)
        if not is_juridical_day(raw, regime=PROCEDURAL):
            return next_juridical_day(raw, regime=PROCEDURAL)
        return raw
    else:
        raw = start_date - timedelta(days=delay_days)
        if not is_juridical_day(raw, regime=PROCEDURAL):
            return prev_juridical_day(raw, regime=PROCEDURAL)
        return raw


def is_juridical_day(d: date, *, regime: Regime = PROCEDURAL) -> bool:
    """Return True if the date is a juridical day on the given calendar.

    Both calendars exclude Saturdays (art. 83 C.p.c.; art. 2879 C.c.Q.) and
    Sundays (art. 61(23) a) L.i.). ``PROCEDURAL`` additionally excludes 26
    December and 2 January every year (art. 82 C.p.c.); ``CIVIL`` does not.
    """
    _check_regime(regime)
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    if d in get_quebec_holidays(d.year, regime=regime):
        return False
    return True


def next_juridical_day(d: date, *, regime: Regime = PROCEDURAL) -> date:
    """Return the next juridical day on or after the given date."""
    current = d
    for _ in range(10):
        if is_juridical_day(current, regime=regime):
            return current
        current += timedelta(days=1)
    return current


def prev_juridical_day(d: date, *, regime: Regime = PROCEDURAL) -> date:
    """Return the previous juridical day on or before the given date."""
    current = d
    for _ in range(10):
        if is_juridical_day(current, regime=regime):
            return current
        current -= timedelta(days=1)
    return current


def last_action_day(deadline: date) -> tuple[date, bool]:
    """The real last day to act before *deadline*, and whether it differs.

    ``prev_juridical_day`` is INCLUSIVE (on-or-before), so on a deadline that
    already falls on a juridical day the last action day IS the deadline and
    the boolean is False. Consumers that surface the date should show it only
    when it differs — otherwise it reads as a duplicated (buggy-looking)
    date. Shared by the dashboard and the MCP get_agenda alert row so the
    two surfaces can never drift.

    Deliberately the PROCEDURAL calendar, even though its one consumer is
    the prescription alert, whose date is computed on the CIVIL one: the act
    that interrupts a prescription is the DÉPÔT of a demande « avant
    l'expiration du délai » (art. 2892 C.c.Q.), the courts do not sit on the
    days art. 82 C.p.c. lists — 26 December and 2 January included — and a
    greffe is closed on a jour férié (e.g. art. 5, Règlement de la Cour du
    Québec; art. 111 al. 2 C.p.c. defers a technological notification made
    on one to the next jour ouvrable). So a prescription
    running out on Friday 26 December 2025 (a jour ouvrable for art. 2879)
    has a last day to FILE of Wednesday the 24th — which is what this
    returns, with ``differs`` True.
    """
    last = prev_juridical_day(deadline, regime=PROCEDURAL)
    return last, last != deadline


def today_mtl() -> date:
    """The current calendar date in Montréal — the ONE clock read.

    Not ``date.today()`` (server-local, undefined on App Engine) and not
    ``datetime.now(timezone.utc).date()``: UTC crosses midnight 4-5 hours
    before Montréal does, so a UTC "today" declares a deadline past during
    the whole evening preceding it.
    """
    return datetime.now(timezone.utc).astimezone(MTL).date()


def _as_date(value) -> Optional[date]:
    """Coerce a stored value to its own calendar date, or None.

    Date-only fields are stored at midnight UTC, so their UTC calendar date
    IS the intended day — never convert them to Montréal (that would shift
    them to the previous day). Only ``today`` is Montréal-based; the
    deadline keeps its own calendar date. Same rule as ``mcp.tools.date_str``.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).date()
    if isinstance(value, date):
        return value
    return None


def effective_due(deadline, *, regime: Regime = PROCEDURAL) -> Optional[date]:
    """The day a deadline is actionable UNTIL: itself, prorogued if needed.

    ``next_juridical_day`` is inclusive, so a deadline already landing on a
    juridical day is returned unchanged — which makes this a NO-OP for every
    computed deadline in the system, PROVIDED the caller names the calendar
    the deadline was computed on: protocol steps and tasks are procedural
    (the default); a prescription date is computed on the ``CIVIL`` calendar
    (art. 2879 C.c.Q.) and must be read back on it, or a prescription
    expiring on a weekday 26 December would be prorogued past the day it
    is acquired. It only moves hand-typed dates that landed on a weekend or
    a holiday of that calendar. The ``regime`` is validated even for an
    undated deadline (a typo must not wait for the first dated row).
    """
    _check_regime(regime)
    when = _as_date(deadline)
    if when is None:
        return None
    return next_juridical_day(when, regime=regime)


def is_past_due(
    deadline, *, today: Optional[date] = None, regime: Regime = PROCEDURAL
) -> bool:
    """True when the PROROGUED deadline fell strictly BEFORE today (Montréal).

    Two rules compose here, both the lawyer's:
    * a deadline falling ON its (effective) day is NOT past due — the day is
      not over and the act can still be posed;
    * a deadline landing on a non-juridical day prorogues to the next
      juridical day before lateness is evaluated (decision 2026-08-02).
      Due Saturday → actionable Monday → past due Tuesday.

    A missing deadline is never past due (an undated task cannot be late).
    ``today`` is injectable so the rule is testable without a clock.
    ``regime`` names the calendar the prorogation runs on — see
    ``effective_due``: a prescription date passes ``CIVIL``.
    """
    when = effective_due(deadline, regime=regime)
    if when is None:
        return False
    return when < (today or today_mtl())


def days_until(
    deadline, *, today: Optional[date] = None, regime: Regime = PROCEDURAL
) -> Optional[int]:
    """Whole days from today (Montréal) to the PROROGUED deadline.

    None when undated. Evaluated on ``effective_due`` so the countdown and
    ``is_past_due`` can never disagree: the count reaches zero on the last
    actionable day and goes negative only once the deadline is truly past —
    never « -1 » on something that is not yet late (the dashboard's old
    evening artifact). Pass the SAME ``regime`` to both, or they can.
    """
    when = effective_due(deadline, regime=regime)
    if when is None:
        return None
    return (when - (today or today_mtl())).days


def add_jours_ouvrables(
    start: date, n: int, *, regime: Regime = PROCEDURAL
) -> date:
    """Add *n* business days: each counted day skips Saturdays, Sundays and
    the holidays of ``regime`` (the same table ``next_juridical_day`` uses via
    ``is_juridical_day``).

    Serves the notice delays expressed in jours ouvrables (art. 3, Loi sur la
    presse — the ``3_jours_ouvrables`` key of ``utils.recours.AVIS_PERIODS``),
    which ``utils.recours`` counts on the ``CIVIL`` calendar: a notice to a
    newspaper is not civil procedure, so art. 82 C.p.c.'s 26 December and
    2 January are ordinary business days for it (the implementation's
    reading — the Loi sur la presse text was not verified; since that avis
    precedes the action, CIVIL yields the EARLIER, less conservative, first
    day to sue, and the choice is the lawyer's). ``n == 0`` returns *start*
    unchanged, even when *start* itself is not a juridical day — but the
    ``regime`` is still validated first: a typo must fail on every call,
    not only on those that happen to count a day.
    """
    _check_regime(regime)
    current = start
    remaining = n
    while remaining > 0:
        current += timedelta(days=1)
        if is_juridical_day(current, regime=regime):
            remaining -= 1
    return current


def get_quebec_holidays(year: int, *, regime: Regime = PROCEDURAL) -> list[date]:
    """Return the dated holidays of *year* on the given calendar, sorted.

    Sundays are jours fériés too (art. 61(23) a) L.i.) but are not dated
    here — ``is_juridical_day`` excludes every weekend first.

    ``CIVIL`` — the dated jours fériés of art. 61(23) L.i., b) to j):
    - 1 January (b)
    - Vendredi saint (c) and lundi de Pâques (d) — Easter-based
    - 24 June, Fête nationale (e) — NO substitute when it is a Sunday:
      unlike f), paragraph e) names none
    - 1 July, or 2 July when 1 July is a Sunday (f) — the ONLY substitute
      day the article provides (1 July stays listed; it is a Sunday then)
    - 1st Monday of September (g), 2nd Monday of October (g.1)
    - 25 December (h) — no substitute either
    - the Monday preceding 25 May: the Sovereign's birthday fixed by the
      Governor General's proclamation (i), also the Journée nationale des
      patriotes fixed by decree (j)

    ``PROCEDURAL`` — the same list PLUS 26 December and 2 January, EVERY
    year, whatever weekday they fall on (art. 82 C.p.c.: « non plus que les
    26 décembre et 2 janvier qui sont, en matière de procédure civile,
    considérés jours fériés »).

    For the Easter calculation, the Anonymous Gregorian algorithm
    (Meeus/Jones/Butcher) gives Easter Sunday; Good Friday = Easter - 2,
    Easter Monday = Easter + 1.
    """
    _check_regime(regime)
    holidays: list[date] = []

    # 1 January — art. 61(23) b) L.i.
    holidays.append(date(year, 1, 1))

    # Easter-based holidays — art. 61(23) c) and d) L.i.
    easter = _easter_sunday(year)
    holidays.append(easter - timedelta(days=2))  # Vendredi saint (Good Friday)
    holidays.append(easter + timedelta(days=1))  # Lundi de Pâques (Easter Monday)

    # The Monday immediately preceding 25 May (= the last Monday on or before
    # 24 May) — art. 61(23) i) and j) L.i.
    may24 = date(year, 5, 24)
    holidays.append(may24 - timedelta(days=may24.weekday()))

    # 24 June — art. 61(23) e) L.i. No substitute: 25 June is an ordinary
    # day even when the 24th is a Sunday (it may be a day OFF for workers
    # under labour legislation; that does not make it a jour férié).
    holidays.append(date(year, 6, 24))

    # 1 July, or 2 July if the 1st is a Sunday — art. 61(23) f) L.i.
    july1 = date(year, 7, 1)
    holidays.append(july1)
    if july1.weekday() == 6:
        holidays.append(date(year, 7, 2))

    # First Monday of September — art. 61(23) g) L.i.
    sept1 = date(year, 9, 1)
    holidays.append(sept1 + timedelta(days=(7 - sept1.weekday()) % 7))

    # Second Monday of October — art. 61(23) g.1) L.i.
    oct1 = date(year, 10, 1)
    first_monday_oct = oct1 + timedelta(days=(7 - oct1.weekday()) % 7)
    holidays.append(first_monday_oct + timedelta(weeks=1))

    # 25 December — art. 61(23) h) L.i. No substitute.
    holidays.append(date(year, 12, 25))

    if regime == PROCEDURAL:
        # Art. 82 C.p.c. — every year, in civil procedure only.
        holidays.extend(date(year, m, d) for m, d in _ART_82_DAYS)

    return sorted(holidays)


def _easter_sunday(year: int) -> date:
    """Compute Easter Sunday for a given year using the Anonymous Gregorian algorithm."""
    # Meeus/Jones/Butcher algorithm
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)
