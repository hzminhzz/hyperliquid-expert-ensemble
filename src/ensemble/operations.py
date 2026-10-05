"""Operations facade: typed plans, authority grants, and idempotency receipts (Contracts O4, Q13, Q16).

Implements safe, bounded control and plan/apply verification.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class AuthorityRole(str, Enum):
    OBSERVER = "OBSERVER"
    RESEARCH_OPERATOR = "RESEARCH_OPERATOR"
    RUNTIME_OPERATOR = "RUNTIME_OPERATOR"
    POLICY_APPROVER = "POLICY_APPROVER"
    EXECUTION_OPERATOR = "EXECUTION_OPERATOR"


AUTHORITY_LEVELS = {
    AuthorityRole.OBSERVER: 1,
    AuthorityRole.RESEARCH_OPERATOR: 2,
    AuthorityRole.RUNTIME_OPERATOR: 3,
    AuthorityRole.POLICY_APPROVER: 4,
    AuthorityRole.EXECUTION_OPERATOR: 5,
}

# Regex to detect malicious prompt injection patterns in user/wallet labels (Q16 Task 6)
HOSTILE_PATTERNS = [
    re.compile(r"ignore\s+previous\s+instructions", re.IGNORECASE),
    re.compile(r"grant\s+(admin|root|execution|authority)", re.IGNORECASE),
    re.compile(r"(sudo|rm\s+-rf|drop\s+table)", re.IGNORECASE),
    re.compile(r"export\s+secrets|show\s+credentials|private_key", re.IGNORECASE),
    re.compile(r"increase\s+risk\s+limit", re.IGNORECASE),
]


class OperationError(Exception):
    """Base error for control and planning violations."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


@dataclass(slots=True, frozen=True)
class ChangePlan:
    plan_id: str
    actor: str
    scope: str
    intent: str
    proposed_diff: dict[str, Any]
    preconditions: dict[str, Any]
    expected_revision: int
    expected_costs: int
    expires_at: datetime
    required_authority: AuthorityRole
    is_financial_trade: bool = False


@dataclass(slots=True, frozen=True)
class CommandReceipt:
    receipt_id: str
    idempotency_key: str
    status: str  # "APPLIED", "REJECTED"
    plan_id: str
    previous_revision: int
    new_revision: int
    effects: dict[str, Any]
    costs: int
    created_at: datetime


@dataclass
class OperationsEngine:
    """Manages immutable plans, scoped grants, and idempotent executions."""

    receipts_by_key: dict[str, tuple[CommandReceipt, str]] = field(default_factory=dict)
    active_plans: dict[str, ChangePlan] = field(default_factory=dict)
    current_revision: int = 1

    def sanitize_label(self, raw_label: str) -> str:
        """Sanitize an untrusted wallet label, neutralizing prompt injections (Q16 Task 6)."""
        for pattern in HOSTILE_PATTERNS:
            if pattern.search(raw_label):
                raise OperationError(
                    "HOSTILE_LABEL_REJECTED",
                    f"Label contains disallowed prompt-injection pattern: '{raw_label}'",
                )
        # Strip dangerous shell/SQL control characters
        return re.sub(r"[^\w\s\-_.:]", "", raw_label).strip()

    def create_plan(
        self,
        actor: str,
        scope: str,
        intent: str,
        proposed_diff: dict[str, Any],
        expected_revision: int,
        expires_at: datetime,
        required_authority: AuthorityRole,
        is_financial_trade: bool = False,
        expected_costs: int = 10,
    ) -> ChangePlan:
        """Create a typed immutable change proposal (O4)."""
        # Invariant: V1 explicitly forbids real financial trading effects
        if is_financial_trade or required_authority == AuthorityRole.EXECUTION_OPERATOR:
            raise OperationError(
                "FINANCIAL_EFFECT_FORBIDDEN",
                "Automated financial trading and order placement are strictly forbidden in V1.",
            )

        # Sanitize intent description
        safe_intent = self.sanitize_label(intent)

        plan_content = json.dumps(
            {
                "actor": actor,
                "scope": scope,
                "intent": safe_intent,
                "diff": proposed_diff,
                "expected_rev": expected_revision,
            },
            sort_keys=True,
        )
        plan_id = f"plan:{hashlib.sha256(plan_content.encode()).hexdigest()[:12]}"

        plan = ChangePlan(
            plan_id=plan_id,
            actor=actor,
            scope=scope,
            intent=safe_intent,
            proposed_diff=proposed_diff,
            preconditions={"expected_revision": expected_revision},
            expected_revision=expected_revision,
            expected_costs=expected_costs,
            expires_at=expires_at,
            required_authority=required_authority,
            is_financial_trade=is_financial_trade,
        )
        self.active_plans[plan_id] = plan
        return plan

    def apply_plan(
        self,
        plan_id: str,
        actor_authority: AuthorityRole,
        idempotency_key: str,
        params: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> CommandReceipt:
        """Apply an approved change plan idempotently (O4, Q13)."""
        current_time = now or datetime.now(UTC)
        params_hash = hashlib.sha256(json.dumps(params or {}, sort_keys=True).encode()).hexdigest()

        # 1. Idempotency check: repeated key with same params returns original receipt
        if idempotency_key in self.receipts_by_key:
            existing_receipt, existing_params_hash = self.receipts_by_key[idempotency_key]
            if existing_params_hash == params_hash:
                return existing_receipt
            raise OperationError(
                "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PARAMS",
                f"Idempotency key '{idempotency_key}' was reused with altered parameters.",
            )

        plan = self.active_plans.get(plan_id)
        if not plan:
            raise OperationError("PLAN_NOT_FOUND", f"Plan '{plan_id}' does not exist.")

        # 2. Authority check: reject privilege escalation
        required_lvl = AUTHORITY_LEVELS[plan.required_authority]
        actual_lvl = AUTHORITY_LEVELS[actor_authority]
        if actual_lvl < required_lvl:
            raise OperationError(
                "PERMISSION_DENIED",
                f"Action requires authority {plan.required_authority.value}, but caller only has {actor_authority.value}.",
            )

        # 3. Plan expiry check
        if current_time > plan.expires_at:
            raise OperationError(
                "PLAN_EXPIRED",
                f"Plan '{plan_id}' expired at {plan.expires_at.isoformat()} (current time {current_time.isoformat()}).",
            )

        # 4. Revision drift check (Q16 Task 5): reject stale expected revision
        if self.current_revision != plan.expected_revision:
            raise OperationError(
                "PRECONDITION_FAILED",
                f"Revision drift detected: plan expected revision {plan.expected_revision}, but current revision is {self.current_revision}.",
            )

        # 5. Apply changes & advance revision
        prev_rev = self.current_revision
        self.current_revision += 1
        receipt_id = f"rcpt:{idempotency_key[:8]}:{self.current_revision}"

        receipt = CommandReceipt(
            receipt_id=receipt_id,
            idempotency_key=idempotency_key,
            status="APPLIED",
            plan_id=plan_id,
            previous_revision=prev_rev,
            new_revision=self.current_revision,
            effects=dict(plan.proposed_diff),
            costs=plan.expected_costs,
            created_at=current_time,
        )

        self.receipts_by_key[idempotency_key] = (receipt, params_hash)
        return receipt
