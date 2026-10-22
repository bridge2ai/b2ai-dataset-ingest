"""The ``when_value`` condition grammar — parse and evaluate value-gated mappings.

A B2AI -> HPO SSSOM row can carry a ``when_value`` extension column that turns the
*term-to-term* mapping into a *value-gated* one: the HPO ``PhenotypicFeature`` is asserted
only for answers that satisfy the condition (see ADR-0002 and ``docs/mapping-conventions.md``).
This module is the **definition + evaluation** layer for that column; it does not touch SSSOM
I/O or the IR (that is :mod:`b2ai_dataset_ingest.mapping.hpo_rules`).

Grammar (one ``when_value`` cell):

- **Comparison** — ``>=1``, ``<=3``, ``>0``, ``<2``, ``==0``, ``!=0``. Numeric operand.
- **String equality** — ``== "Checked"`` / ``!= "Unchecked"`` (single or double quotes; a
  bare unquoted token is also accepted as a string, e.g. ``== Checked``).
- **Membership** — ``in {1,2,3}`` (numbers) or ``in {"a","b"}`` (strings).
- **Conjunction** — ``>=1 & <=3`` (``&``-separated; every term must hold).

A :class:`ValueCondition` is a conjunction of :class:`Comparison` atoms. The operators mirror
LinkML ``slot_conditions`` value metaslots so the condition has a language-neutral definition
even though we evaluate it in hand-written Python (LinkML's ``equals_expression``/``--infer``
executor is broken on Python 3.12+ via the removed ``ast.Num`` — ADR-0002 decision 2):

    >= n        minimum_value              == n   equals_number     in {strings}  equals_string_in
    <= n        maximum_value              == s   equals_string
    > n / < n   (exclusive bounds)         != x   (negated equality; a LinkML extension)

Evaluation is against an :class:`Answer`, which carries both the raw cell and the ordinal the
reader resolved for it: numeric operators compare against the ordinal (falling back to a numeric
raw cell), string operators compare against the raw cell (case-insensitively, matching the rest
of the pipeline's answer handling).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Two-char operators must be tried before their one-char prefixes (``>=`` before ``>``).
_COMPARISON_OPS = ("==", "!=", ">=", "<=", ">", "<")
_ORDERING_OPS = frozenset({">=", "<=", ">", "<"})
_MEMBERSHIP_RE = re.compile(r"^in\s*\{(.*)\}$", re.IGNORECASE)


class ConditionParseError(ValueError):
    """Raised when a ``when_value`` expression is not well-formed."""


@dataclass(frozen=True)
class Answer:
    """One answered cell, as both the raw string and the ordinal the reader resolved.

    ``ordinal`` is the integer score (``None`` if the answer resolved against no choice/scale);
    ``raw`` is the verbatim (stripped) cell. Numeric conditions read ``ordinal``; string
    conditions read ``raw``.
    """

    raw: str
    ordinal: int | None = None


@dataclass(frozen=True)
class Comparison:
    """One atomic term of a condition: an operator plus its operand.

    ``operand`` is a ``float``/``int`` for a numeric term, a ``str`` for a string term, or a
    ``tuple`` of those for ``in``. The operand's Python type decides numeric-vs-string matching.
    """

    op: str
    operand: float | int | str | tuple

    def matches(self, answer: Answer) -> bool:
        if self.op == "in":
            return any(_match_scalar("==", item, answer) for item in self.operand)
        return _match_scalar(self.op, self.operand, answer)


@dataclass(frozen=True)
class ValueCondition:
    """A parsed ``when_value`` expression: a conjunction of :class:`Comparison` atoms."""

    comparisons: tuple[Comparison, ...]
    raw: str

    def matches(self, answer: Answer) -> bool:
        """True iff *every* comparison holds for ``answer`` (an empty condition never matches)."""
        return bool(self.comparisons) and all(c.matches(answer) for c in self.comparisons)


# --------------------------------------------------------------------------- parsing


def parse_condition(text: str) -> ValueCondition:
    """Parse a ``when_value`` expression into a :class:`ValueCondition`.

    Raises :class:`ConditionParseError` for an empty or malformed expression. Callers that
    treat an *absent* condition as "no gate" should check for an empty string first — an empty
    ``when_value`` is an inert semantic mapping, not a condition, and is never parsed here.
    """
    raw = text.strip()
    if not raw:
        raise ConditionParseError("empty condition")
    comparisons: list[Comparison] = []
    for term in raw.split("&"):
        term = term.strip()
        if not term:
            raise ConditionParseError(f"empty term in {raw!r}")
        comparisons.append(_parse_term(term, raw))
    return ValueCondition(comparisons=tuple(comparisons), raw=raw)


def _parse_term(term: str, raw: str) -> Comparison:
    membership = _MEMBERSHIP_RE.match(term)
    if membership:
        items = _parse_set(membership.group(1), raw)
        return Comparison(op="in", operand=items)
    for op in _COMPARISON_OPS:
        if term.startswith(op):
            operand = _parse_operand(term[len(op) :].strip(), raw)
            if op in _ORDERING_OPS and not isinstance(operand, (int, float)):
                raise ConditionParseError(
                    f"operator {op!r} needs a numeric operand in {raw!r}, got {operand!r}"
                )
            return Comparison(op=op, operand=operand)
    raise ConditionParseError(f"unrecognized term {term!r} in {raw!r}")


def _parse_set(body: str, raw: str) -> tuple:
    items = [part.strip() for part in body.split(",")]
    if not any(items) or "" in items:
        raise ConditionParseError(f"malformed set in {raw!r}")
    return tuple(_parse_operand(item, raw) for item in items)


def _parse_operand(text: str, raw: str) -> float | int | str:
    """A number if it parses as one, else the unquoted string literal."""
    if not text:
        raise ConditionParseError(f"missing operand in {raw!r}")
    if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
        return text[1:-1]
    number = _as_number(text)
    return number if number is not None else text


# ------------------------------------------------------------------------ evaluation


def _match_scalar(op: str, operand: float | int | str, answer: Answer) -> bool:
    if isinstance(operand, (int, float)) and not isinstance(operand, bool):
        value = answer.ordinal if answer.ordinal is not None else _as_number(answer.raw)
        if value is None:
            return False
        return _compare_numeric(op, float(value), float(operand))
    # string operand: only equality/inequality is meaningful (ordering is rejected at parse time)
    left = answer.raw.strip().lower()
    right = str(operand).strip().lower()
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    return False


def _compare_numeric(op: str, left: float, right: float) -> bool:
    if op == ">=":
        return left >= right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == "<":
        return left < right
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    return False


# ------------------------------------------------------------- subject conditions

#: The ``Individual`` fields a ``when_subject`` term may name. ``sex`` is the phenopacket
#: enum spelling (FEMALE/MALE/OTHER_SEX/UNKNOWN_SEX); ``age`` is whole years at the
#: observation. Closed on purpose: a gate that could reference an arbitrary field would
#: silently stop firing when a reader renamed one.
SUBJECT_FIELDS = frozenset({"sex", "age"})

_SUBJECT_TERM_RE = re.compile(r"^([A-Za-z_]+)\s*(==|!=|>=|<=|>|<)\s*(.+)$")


@dataclass(frozen=True)
class SubjectCondition:
    """A parsed ``when_subject`` expression: ``field`` -> condition, every field must hold.

    This is the stratification layer for a value gate: a reference interval that differs by
    sex or age is written as several rows, each with the same ``when_value`` shape and a
    ``when_subject`` naming who it applies to (``sex == "FEMALE"``, ``sex == "MALE" & age >=
    50``). A row whose subject condition names a field the participant has no value for
    does **not** fire -- an unknown sex must never satisfy a female-only gate.
    """

    conditions: tuple[tuple[str, ValueCondition], ...]
    raw: str

    def matches(self, subject: dict[str, str] | None) -> bool:
        if not self.conditions:
            return True
        if not subject:
            return False
        for field, condition in self.conditions:
            raw = (subject.get(field) or "").strip()
            if not raw:
                return False
            if not condition.matches(Answer(raw=raw, ordinal=None)):
                return False
        return True


def parse_subject_condition(text: str) -> SubjectCondition:
    """Parse a ``when_subject`` expression: ``&``-separated ``<field> <op> <operand>`` terms.

    Fields are restricted to :data:`SUBJECT_FIELDS`. Each term reuses the ``when_value``
    operator grammar, so ``sex == "FEMALE"`` and ``age >= 50`` parse exactly as a value gate
    would, and the same evaluator compares them. Raises :class:`ConditionParseError` for an
    empty expression, an unknown field, or a malformed term.
    """
    raw = text.strip()
    if not raw:
        raise ConditionParseError("empty subject condition")
    parsed: list[tuple[str, ValueCondition]] = []
    for term in raw.split("&"):
        term = term.strip()
        match = _SUBJECT_TERM_RE.match(term)
        if not match:
            raise ConditionParseError(f"unrecognized subject term {term!r} in {raw!r}")
        field, op, operand = match.group(1).lower(), match.group(2), match.group(3).strip()
        if field not in SUBJECT_FIELDS:
            raise ConditionParseError(
                f"unknown subject field {field!r} in {raw!r}; allowed: {sorted(SUBJECT_FIELDS)}"
            )
        parsed.append((field, parse_condition(f"{op} {operand}")))
    return SubjectCondition(conditions=tuple(parsed), raw=raw)


def _as_number(text: str) -> float | int | None:
    """Parse ``"3"`` -> ``3`` and ``"3.5"`` -> ``3.5``; ``None`` if not numeric.

    An integer-valued float (``"3.0"``) is returned as an ``int`` so ``in {1,2,3}`` membership
    and ``== 0`` behave as a curator expects for ordinal codes.
    """
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number
