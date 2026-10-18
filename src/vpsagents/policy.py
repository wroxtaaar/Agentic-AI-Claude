"""Autonomy policy. The ONLY place that decides what may happen without a human.

Tiers
  0  observe     read-only jobs only (investigate, incident, security review)
  1  propose     agent may work, but even *starting* a change needs approval; apply/deploy/restart need approval
  2  assisted    agent works + verifies automatically; apply/deploy/restart need approval
  3  trusted     apply/deploy/restart run automatically IF preconditions hold (verified, reviewed, healthcheck);
                 failed deploys roll back automatically
Some actions are denied at every tier.
"""
from __future__ import annotations

from dataclasses import dataclass

# Actions
START_READONLY = "start_readonly"
START_CHANGE = "start_change"
APPLY = "apply"          # merge the job branch into the project checkout
DEPLOY = "deploy"
RESTART = "restart"      # restart one of the project's containers

# Never automatic, never approvable through this system.
NEVER = frozenset({"force_push", "delete_branch", "delete_project", "read_secrets", "touch_production_data"})

AUTO, APPROVAL, DENY = "auto", "approval", "deny"


@dataclass(frozen=True)
class Decision:
    mode: str
    reason: str


_MATRIX = {
    #                tier0     tier1       tier2       tier3
    START_READONLY: (AUTO,     AUTO,       AUTO,       AUTO),
    START_CHANGE:   (DENY,     APPROVAL,   AUTO,       AUTO),
    APPLY:          (DENY,     APPROVAL,   APPROVAL,   AUTO),
    DEPLOY:         (DENY,     APPROVAL,   APPROVAL,   AUTO),
    RESTART:        (DENY,     APPROVAL,   APPROVAL,   AUTO),
}


def decide(tier: int, action: str) -> Decision:
    if action in NEVER:
        return Decision(DENY, f"'{action}' is never permitted")
    if action not in _MATRIX:
        return Decision(DENY, f"unknown action '{action}'")
    if not 0 <= tier <= 3:
        return Decision(DENY, f"invalid tier {tier}")
    mode = _MATRIX[action][tier]
    return Decision(mode, f"tier {tier}: {action} -> {mode}")


def downgrade_if_unsafe(decision: Decision, *, action: str, verified: bool = True,
                        review_ok: bool = True, has_healthcheck: bool = True,
                        deploy_ok: bool = True) -> Decision:
    """An AUTO decision is only honoured when its safety preconditions hold; otherwise ask a human."""
    if decision.mode != AUTO:
        return decision
    if action == APPLY and not (verified and review_ok):
        return Decision(APPROVAL, "auto-apply needs passing verification and an APPROVE review verdict")
    if action in (DEPLOY, RESTART) and not has_healthcheck:
        return Decision(APPROVAL, "auto-deploy/restart needs a health check to detect failure")
    if action == DEPLOY and not deploy_ok:
        return Decision(APPROVAL, "auto-deploy needs a successful apply of a verified change")
    return decision
