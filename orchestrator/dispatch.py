"""Provider-neutral dispatch boundaries; no model or paid provider is included."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from reviewer.contract import ReviewResult, ReviewerContext
from worker.contract import AttemptResult, WorkerContext

from .contract import FailureCategory


class DispatchError(RuntimeError):
    def __init__(self, category: FailureCategory, message: str):
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class WorkerDispatchRequest:
    context: WorkerContext
    attempt: int
    worker_identity: str
    claim_token: str
    prior_review_feedback: tuple[str, ...] = ()


class WorkerDispatcher(Protocol):
    def dispatch(self, request: WorkerDispatchRequest) -> AttemptResult: ...


@dataclass(frozen=True)
class ReviewerDispatchRequest:
    context: ReviewerContext
    reviewer_identity: str
    claim_token: str


class ReviewerDispatcher(Protocol):
    def dispatch(self, request: ReviewerDispatchRequest) -> ReviewResult: ...
