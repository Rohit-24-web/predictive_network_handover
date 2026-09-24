"""SIMULATED multi-orbit network paths and the reactive/predictive experiment harness.

Nothing in this package is a real satellite measurement. Path behaviour is generated
from the Phase 1 simulator's published parameters (artifacts/feature_config.json ->
orbit_params) so that telemetry stays inside the model's training distribution.
"""
from app.simulation.network import ScenarioRecord, generate_scenario
from app.simulation.scenarios import SCENARIOS, list_scenarios
from app.simulation.runner import RunResult, run_experiment, run_mode
from app.simulation.metrics import RunMetrics, compare, compute_metrics

__all__ = [
    "generate_scenario", "ScenarioRecord", "SCENARIOS", "list_scenarios",
    "run_mode", "run_experiment", "RunResult",
    "compute_metrics", "compare", "RunMetrics",
]
