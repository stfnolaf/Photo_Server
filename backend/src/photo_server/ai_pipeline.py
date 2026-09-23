"""Dependency metadata for the photo AI pipeline.

Stages declare artifacts they require and produce. The scheduler can run
stages whose requirements are satisfied concurrently and joins them before a
downstream stage. Keeping this metadata separate from the face/VLM clients
allows later stages—such as people-aware semantic analysis—to declare a face
artifact dependency without changing queue semantics.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PipelineStage:
    name: str
    requires: frozenset[str]
    produces: frozenset[str]


PIPELINE_STAGES = (
    PipelineStage("face", frozenset({"preview"}), frozenset({"faces"})),
    PipelineStage("semantic", frozenset({"preview"}), frozenset({"semantic"})),
    PipelineStage(
        "publish",
        frozenset({"faces", "semantic"}),
        frozenset({"analysis-run"}),
    ),
)


def stage(name: str) -> PipelineStage:
    for definition in PIPELINE_STAGES:
        if definition.name == name:
            return definition
    raise KeyError(f"Unknown AI pipeline stage: {name}")


def can_run(name: str, available: set[str]) -> bool:
    """Return whether all artifact dependencies for a stage are available."""
    return stage(name).requires <= available


def independent(left: str, right: str, available: set[str]) -> bool:
    """Return whether two stages are both runnable from the same artifacts."""
    return can_run(left, available) and can_run(right, available)
