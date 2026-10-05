"""hyperliquid-expert-ensemble package."""

from .adviser import (
    AccountAdvice,
    AccountRulebook,
    AccountState,
    AdviceStatus,
    InstrumentSpec,
    LossModel,
    generate_account_advice,
)
from .checkpoint import EvaluationJob, JobStore
from .clustering import ClusterArtifact, complete_link_clustering
from .consensus import (
    CausalChangeCategory,
    ConsensusTarget,
    compute_equal_budget_consensus,
    compute_hierarchical_cluster_consensus,
)
from .evaluation import CandidateEvaluation, evaluate_baseline_vs_candidate
from .explain import explain_blocker, explain_target
from .handoff import CompactHandoff, IncidentRunbook, RunbookRegistry, generate_compact_handoff
from .notifier_adapter import format_advice_html, should_notify_advice
from .operations import AuthorityRole, ChangePlan, CommandReceipt, OperationError, OperationsEngine
from .posture import EligibilityState, ExpertPosture, compute_posture
from .projection import ProjectedPosition, ProjectionStore, Worldview
from .replay import ReplayManifest, ReplayResult, ReplayRunner
from .similarity import SimilarityMetric, compute_pairwise_distance

__all__ = [
    "AccountAdvice",
    "AccountRulebook",
    "AccountState",
    "AdviceStatus",
    "AuthorityRole",
    "CandidateEvaluation",
    "CausalChangeCategory",
    "ChangePlan",
    "ClusterArtifact",
    "CommandReceipt",
    "CompactHandoff",
    "ConsensusTarget",
    "EligibilityState",
    "EvaluationJob",
    "ExpertPosture",
    "IncidentRunbook",
    "InstrumentSpec",
    "JobStore",
    "LossModel",
    "OperationError",
    "OperationsEngine",
    "ProjectedPosition",
    "ProjectionStore",
    "ReplayManifest",
    "ReplayResult",
    "ReplayRunner",
    "RunbookRegistry",
    "SimilarityMetric",
    "Worldview",
    "complete_link_clustering",
    "compute_equal_budget_consensus",
    "compute_hierarchical_cluster_consensus",
    "compute_pairwise_distance",
    "compute_posture",
    "evaluate_baseline_vs_candidate",
    "explain_blocker",
    "explain_target",
    "format_advice_html",
    "generate_account_advice",
    "generate_compact_handoff",
    "should_notify_advice",
]
