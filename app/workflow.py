from dataclasses import dataclass, field
from typing import Any


class WorkflowStep:
    """Agent 主流程阶段常量，先用轻量状态机承接后续 LangGraph 迁移。"""

    CLASSIFY = "classify"
    RISK_GATE = "risk_gate"
    SELECT_TOOL = "select_tool"
    RETRIEVE = "retrieve"
    CALL_TOOL = "call_tool"
    HUMAN_HANDOFF = "human_handoff"
    RESPOND = "respond"
    FALLBACK = "fallback"


@dataclass
class AgentWorkflowState:
    """单次 Agent 执行状态。

    当前只输出可观测状态；后续接 LangGraph checkpoint 时，
    可以把 trace_id/steps/status 直接映射为图节点和恢复点。
    """

    trace_id: str
    intent: str
    risk_level: str
    steps: list[str] = field(default_factory=list)
    status: str = "running"

    def add(self, step: str) -> "AgentWorkflowState":
        self.steps.append(step)
        return self

    def complete(self) -> "AgentWorkflowState":
        self.status = "completed"
        return self

    def fallback(self) -> "AgentWorkflowState":
        self.status = "fallback"
        self.add(WorkflowStep.FALLBACK)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "traceId": self.trace_id,
            "intent": self.intent,
            "riskLevel": self.risk_level,
            "steps": self.steps,
            "status": self.status,
        }


def completed_workflow(
    *,
    trace_id: str,
    intent: str,
    risk_level: str,
    steps: list[str],
) -> dict[str, Any]:
    state = AgentWorkflowState(trace_id=trace_id, intent=intent, risk_level=risk_level)
    for step in steps:
        state.add(step)
    return state.complete().to_dict()
