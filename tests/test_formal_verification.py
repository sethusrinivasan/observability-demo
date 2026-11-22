"""
tests/test_formal_verification.py
Pytest integration for Formal Methods & TLA+ Verification Engine.
"""
import os
import sys
import pytest

# Ensure repository root is on sys.path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from formal_spec.verifier import (
    TLCModelChecker,
    CounterfactualFalsificationEngine,
    RuntimeTraceRefinementVerifier,
    EXPECTED_GROUND_TRUTH,
    AWS_CANARY_URL,
    LIVE_SERVICES
)


def test_tla_drift_proof_model_checking():
    """Formally verifies ObservabilityDriftProof.tla using TLC model checker."""
    res = TLCModelChecker.run_spec("ObservabilityDriftProof")
    assert res["verified"] is True, f"TLC verification failed: {res.get('raw_output')}"
    assert res["distinct_states"] > 0
    assert res["exit_code"] == 0


def test_tla_architecture_model_checking():
    """Formally verifies ObservabilityArchitecture.tla using TLC model checker."""
    res = TLCModelChecker.run_spec("ObservabilityArchitecture")
    assert res["verified"] is True, f"TLC verification failed: {res.get('raw_output')}"
    assert res["states_generated"] > 50000
    assert res["exit_code"] == 0


def test_counterfactual_drift_rejection():
    """Proves that adversarial/spoofed microservice identity payloads are mathematically rejected."""
    res = CounterfactualFalsificationEngine.run_falsification_tests()
    assert res["verified"] is True
    for case in res["cases"]:
        assert case["falsified_and_blocked"] is True


def test_runtime_polyglot_microservice_refinement():
    """Validates GroundTruthIdentityInvariant against live AWS Lambda microservices.
    
    Verifies that every responding microservice strictly adheres to the tri-factor
    identity invariants: authentic language, expected process_name, and /proc/self/comm.
    """
    try:
        res = RuntimeTraceRefinementVerifier.verify_all_polyglot_microservice_invariants()
    except Exception as e:
        pytest.skip(f"Live AWS environment check error: {e}")

    services = res["services"]
    verified_count = 0
    degraded = []

    for svc, details in services.items():
        if details.get("status") == "HTTP_ERROR":
            degraded.append((svc, details.get("code")))
            continue

        # Strict ground-truth assertions on live microservice responses
        exp = EXPECTED_GROUND_TRUTH[svc]
        assert details["reported_language"] == exp["lang"], f"{svc} reported drifted language: {details['reported_language']}"
        assert details["reported_process"] == exp["proc"], f"{svc} reported drifted process: {details['reported_process']}"
        assert details["reported_comm"] == exp["comm"], f"{svc} reported drifted linux_comm: {details['reported_comm']}"
        assert details["pid"] > 0, f"{svc} reported invalid PID: {details['pid']}"
        assert details["verified"] is True
        verified_count += 1

    # At least 8 of the 9 polyglot services must be 100% verified live
    assert verified_count >= 8, f"Too many degraded services ({degraded}); verified: {verified_count}/9"
