"""hyperliquid-expert-ensemble package."""

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
from .operations import AuthorityRole, ChangePlan, CommandReceipt, OperationError, OperationsEngine
from .posture import EligibilityState, ExpertPosture, compute_posture
from .projection import ProjectedPosition, ProjectionStore, Worldview
from .replay import ReplayManifest, ReplayResult, ReplayRunner
from .similarity import SimilarityMetric, compute_pairwise_distance

__all__ = [
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
    "JobStore",
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
    "generate_compact_handoff",
]
