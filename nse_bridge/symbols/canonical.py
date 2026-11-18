"""Canonical instrument identifiers (BRIDGE-LOCAL scheme).

A canonical ID names an instrument independently of any broker's
naming quirks, so the server can ask for ``XAU/USD`` without knowing
whether this broker calls it ``XAUUSD``, ``XAUUSDm``, ``XAUUSD.a``
or ``GOLD#``.

Scheme (``BASE/QUOTE``)
-----------------------
``<BASE>/<QUOTE>`` — both parts uppercase, slash-separated:

* ``EUR/USD`` — euro vs US dollar.
* ``XAU/USD`` — gold vs US dollar.
* ``BTC/USD`` — bitcoin vs US dollar.

``BASE`` is the underlying instrument code: an ISO 4217 currency code
for FX and metals (``EUR``, ``USD``, ``XAU``, ``XAG``), the asset code
for crypto (``BTC``, ``ETH``), or — for indices, stocks and other
non-currency instruments — the root code the server defines
(``US30``, ``SPX500``). ``QUOTE`` is the quote currency code. The
Bridge does NOT invent a split for an unseparated code it cannot
parse: a 6-letter all-alpha code (``EURUSD``) splits 3+3; anything
else without a slash (``US30``, ``SPX500USD``) is UNPARSEABLE and
``resolve()`` reports ``not_found`` with that reason, rather than
guessing where the base ends and the quote begins.

Accepted input spellings are normalized to the canonical form:
``eurusd`` → ``EUR/USD``; ``eur-usd``, ``eur_usd``, ``eur usd``,
``eur/usd`` → ``EUR/USD``. Normalization never changes meaning — it
only fixes case and separators.

BRIDGE-LOCAL status (contract dependency D7)
--------------------------------------------
This scheme is a BRIDGE-LOCAL interface. CONTRACT v1 defines NO
canonical instrument format and NO symbol-inventory sync channel —
the only symbol data path the contract defines is the per-symbol
chart protocol (§17.1), and no sync kind carries symbols. Until the
contract defines a canonical format, the server and the Bridge must
agree on ``BASE/QUOTE`` out of band; if the contract later defines a
different format, this module is the single place that changes. Do
NOT present a canonical ID to the server as a contract format.

Broker-name matching
--------------------
A broker name matches a canonical ID when, after normalization
(uppercase, all non-alphanumeric characters removed), it equals
``BASE+QUOTE`` or ``BASE+QUOTE`` plus a short alphabetic broker
suffix (``M``, ``PRO``, ``ECN``, ``A``, … — see
:func:`is_broker_suffix`). The currency guard applies on top: when
the broker reports ``currency_base`` / ``currency_profit``, they must
equal ``BASE`` / ``QUOTE`` or the name is not a candidate, however
similar it looks.

One documented broker alias exists: some brokers name gold ``GOLD``
instead of ``XAU`` (spec §4 lists ``GOLD`` / ``GOLD#`` as gold
symbols). ``BROKER_BASE_ALIASES`` maps that alias to ``XAU``. It is
an explicit, reviewable table — not a heuristic — and it is the only
alias. Anything else must match by name + currency guard.
"""

from __future__ import annotations

#: Broker base-name aliases: broker root code -> canonical base code.
#: Explicit table, nothing more. ``GOLD`` is the gold alias spec §4
#: itself lists as a broker naming variant.
BROKER_BASE_ALIASES: dict[str, str] = {
    "GOLD": "XAU",
}

#: Longest broker suffix accepted after the ``BASE+QUOTE`` core.
#: Real broker suffixes are short — ``M``, ``PRO``, ``ECN``, ``RAW``,
#: ``STD``, ``MICRO`` (5) — so a longer tail is not a broker suffix,
#: it is a different instrument name (e.g. a dated future).
MAX_BROKER_SUFFIX_LEN = 5


def parse_canonical_id(raw: str | None) -> tuple[str, str] | None:
    """Parse a canonical ID into ``(BASE, QUOTE)``, or ``None``.

    Accepts ``BASE/QUOTE`` (any case, ``-``/``_``/space as separator)
    and bare 6-letter all-alpha codes (``EURUSD`` → ``EUR/USD``).
    Returns ``None`` for anything else — empty input, a single part,
    three parts, or a bare code whose split cannot be determined
    without guessing.
    """
    if raw is None:
        return None
    text = str(raw).strip().upper()
    if not text:
        return None
    # Normalise separators to "/" first, then split.
    for sep in ("-", "_", " "):
        text = text.replace(sep, "/")
    parts = [p for p in text.split("/") if p]
    if len(parts) == 2 and all(p.isalnum() for p in parts):
        return (parts[0], parts[1])
    if len(parts) == 1 and len(parts[0]) == 6 and parts[0].isalpha():
        return (parts[0][:3], parts[0][3:])
    return None


def normalize_canonical_id(raw: str | None) -> str | None:
    """Canonical ``BASE/QUOTE`` form of ``raw``, or ``None`` if unparseable."""
    parsed = parse_canonical_id(raw)
    if parsed is None:
        return None
    return f"{parsed[0]}/{parsed[1]}"


def normalize_broker_name(name: str) -> str:
    """Broker symbol name reduced to uppercase alphanumerics only.

    ``XAUUSD.a`` → ``XAUUSDA``; ``GOLD#`` → ``GOLD``;
    ``EURUSD-ECN`` → ``EURUSDECN``. Separators carry no meaning for
    matching — the suffix test and the currency guard decide.
    """
    return "".join(ch for ch in str(name).upper() if ch.isalnum())


def canonical_base_for_broker_base(broker_base: str) -> str:
    """Canonical base code for a broker's root code (alias-aware)."""
    return BROKER_BASE_ALIASES.get(str(broker_base).upper(), str(broker_base).upper())


def broker_cores_for(base: str, quote: str) -> list[str]:
    """Broker-name cores that can denote canonical ``BASE/QUOTE``.

    The primary core is ``BASE+QUOTE`` (``XAUUSD``). When ``BASE`` is
    the canonical side of a documented alias, the alias cores are
    added: the bare alias (``GOLD`` — some brokers name the whole
    instrument without the quote currency) and the alias plus quote
    (``GOLDUSD``). Order is stable: primary first, then alias cores.
    """
    cores = [f"{base}{quote}"]
    for alias, canonical in BROKER_BASE_ALIASES.items():
        if canonical == base:
            for alias_core in (alias, f"{alias}{quote}"):
                if alias_core not in cores:
                    cores.append(alias_core)
    return cores


def is_broker_suffix(tail: str) -> bool:
    """True when ``tail`` looks like a broker suffix, not a new instrument.

    A suffix is short (≤ :data:`MAX_BROKER_SUFFIX_LEN`) and alphabetic
    only — ``M``, ``PRO``, ``ECN``, ``A``. Digits or a long tail mean
    the name is a different symbol (e.g. a dated future), not a
    suffixed variant of the canonical instrument.
    """
    return 0 < len(tail) <= MAX_BROKER_SUFFIX_LEN and tail.isalpha()


def broker_name_matches(name: str, base: str, quote: str) -> bool:
    """Name-only test: does broker symbol ``name`` denote ``BASE/QUOTE``?

    True when the normalized name equals a broker core, or a core plus
    a broker suffix. This is the NAME test only — the resolver applies
    the currency guard (``currency_base``/``currency_profit``) on top,
    because a similar name with different currencies is a different
    instrument.
    """
    normalized = normalize_broker_name(name)
    for core in broker_cores_for(base, quote):
        if normalized == core:
            return True
        if normalized.startswith(core) and is_broker_suffix(normalized[len(core) :]):
            return True
    return False
