"""Job lifecycle."""
QUEUED = "queued"
RUNNING = "running"
AWAITING = "awaiting_approval"
DONE = "done"
FAILED = "failed"
REJECTED = "rejected"
CANCELLED = "cancelled"

TERMINAL = frozenset({DONE, FAILED, REJECTED, CANCELLED})

TRANSITIONS = {
    QUEUED: {RUNNING, CANCELLED, FAILED},
    RUNNING: {AWAITING, DONE, FAILED, CANCELLED, QUEUED},
    AWAITING: {QUEUED, REJECTED, CANCELLED, FAILED},
    DONE: set(), FAILED: set(), REJECTED: set(), CANCELLED: set(),
}

# Stages a job moves through (resting points are re-entered after an approval).
STAGES = ("start", "plan", "work", "verify", "review", "apply", "deploy", "restart")

# job kinds
READONLY_KINDS = frozenset({"investigate", "incident", "security_review"})
CHANGE_KINDS = frozenset({"change"})
OPS_KINDS = frozenset({"restart"})
ALL_KINDS = READONLY_KINDS | CHANGE_KINDS | OPS_KINDS


class InvalidTransition(Exception):
    pass
