"""
test_policy.py -- pytest suite for policy.rego (package agent.guardrail).

Run with:  python3 -m pytest test_policy.py -v

Each test builds a policy_input dict exactly as the harnesses do, calls the
real `opa eval` binary against the real policy.rego file (no mocking of OPA
itself -- this exercises the actual Rego logic, not a re-implementation of
it in Python), and asserts on the returned decision + reasons.

Two sections:
  1. Intended-behavior tests -- every decision path the policy is designed
     to take (deny / escalate / allow), one test per path, each with a
     one-line rationale for why that input should produce that decision.
  2. Known-gap tests -- documents the fail-open behavior found during
     production-readiness review (see conversation history). These are
     NOT marked xfail: they assert what the policy ACTUALLY does today,
     so a future fix to close the fail-open gap will make these tests
     fail loudly and force someone to update them deliberately, instead
     of the regression silently going unnoticed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

WORK = Path(__file__).parent
OPA_BIN = Path("/home/claude/opa")
POLICY = WORK / "policy.rego"


def eval_policy(policy_input: dict) -> tuple[str, list[str]]:
    proc = subprocess.run(
        [str(OPA_BIN), "eval", "-d", str(POLICY), "-I", "--format", "json",
         "data.agent.guardrail"],
        input=json.dumps(policy_input), capture_output=True, text=True, check=True,
    )
    doc = json.loads(proc.stdout)["result"][0]["expressions"][0]["value"]
    return doc["decision"], doc.get("reasons", [])


def base_input(**overrides) -> dict:
    """A fully-populated, otherwise-benign input. Tests override only the
    field(s) relevant to what they're checking, so each test's intent is
    visible from its overrides alone."""
    pi = {
        "injection_surface": "direct_user_request",
        "proposed_tool": {"name": "get_balance", "sensitivity_tier": "read"},
        "tool_already_in_flight": False,
        "argument_pivot": False,
        "user_intent_match": True,
        "definition_layer_flag": False,
    }
    pi.update(overrides)
    return pi


# ===========================================================================
# SECTION 1 -- intended behavior, one test per decision path
# ===========================================================================

class TestPoisonedDefinition:
    def test_poisoned_definition_denies_regardless_of_everything_else(self):
        """A poisoned tool definition is an outright deny -- takes priority
        over every other signal, even a low-tier tool with full intent match."""
        pi = base_input(definition_layer_flag=True)
        decision, reasons = eval_policy(pi)
        assert decision == "deny"
        assert "poisoned_tool_definition" in reasons


class TestReadTier:
    def test_read_tier_untrusted_no_pivot_allows(self):
        """Read-tier tools are never gated by this policy, even from an
        untrusted surface -- a known, documented scope limitation (data
        exfiltration via read-tool parameters is out of scope here)."""
        pi = base_input(
            injection_surface="tool_output",
            proposed_tool={"name": "web_search", "sensitivity_tier": "read"},
            user_intent_match=False,
        )
        decision, reasons = eval_policy(pi)
        assert decision == "allow"
        assert "read_tier_or_same_tool_or_trusted_source" in reasons


class TestPrimaryPivotGate:
    def test_untrusted_new_tool_no_intent_match_denies(self):
        """The core mechanism: untrusted surface + write-or-above tier +
        tool never used this session + no evidence the user asked for it
        -> deny. This is the 129/133 pattern from AgentInjectionBench."""
        pi = base_input(
            injection_surface="tool_output",
            proposed_tool={"name": "send_money", "sensitivity_tier": "irreversible"},
            tool_already_in_flight=False,
            user_intent_match=False,
        )
        decision, reasons = eval_policy(pi)
        assert decision == "deny"
        assert "untrusted_source_tool_pivot_no_intent_match" in reasons

    def test_untrusted_new_tool_with_intent_match_escalates_not_denies(self):
        """Same pivot, but the user's own message plausibly justifies it
        -> escalate (surface for review), not an outright block. This is
        the injection_task_5 case: 'update my address' legitimately maps
        to update_user_info even though it's a same-session pivot."""
        pi = base_input(
            injection_surface="tool_output",
            proposed_tool={"name": "update_user_info", "sensitivity_tier": "write"},
            tool_already_in_flight=False,
            user_intent_match=True,
        )
        decision, reasons = eval_policy(pi)
        assert decision == "escalate"
        assert "untrusted_source_tool_pivot_intent_match_needs_escalation" in reasons

    def test_trusted_surface_bypasses_the_gate_entirely(self):
        """A first-turn request straight from the user, before any tool
        output has been read, is not 'untrusted' -- the gate never engages
        even for an irreversible-tier tool with no other signals."""
        pi = base_input(
            injection_surface="direct_user_request",
            proposed_tool={"name": "send_money", "sensitivity_tier": "irreversible"},
            tool_already_in_flight=False,
            user_intent_match=False,
        )
        decision, reasons = eval_policy(pi)
        assert decision == "allow"


class TestContinuationExemption:
    def test_write_tier_repeat_call_is_exempt(self):
        """A write-tier tool already used once this session gets a pass on
        a repeat call, even from an untrusted surface with no intent match
        -- this is the deliberate false-positive suppression for normal
        multi-step tasks (e.g. AgentDojo's legitimate multi-part transfers)."""
        pi = base_input(
            injection_surface="tool_output",
            proposed_tool={"name": "update_user_info", "sensitivity_tier": "write"},
            tool_already_in_flight=True,
            argument_pivot=False,
            user_intent_match=False,
        )
        decision, reasons = eval_policy(pi)
        assert decision == "allow"

    def test_execute_tier_repeat_call_is_NOT_exempt(self):
        """The high-risk-tier fix: execute/irreversible tier tools do NOT
        get the continuation exemption, even on a repeat call. This closes
        the 'legitimize the tool early, weaponize it later' multi-turn
        evasion found in AgentInjectionBench's multi_turn_stateful category
        (AIB-00064, AIB-00072, AIB-00164)."""
        pi = base_input(
            injection_surface="tool_output",
            proposed_tool={"name": "shell_exec", "sensitivity_tier": "execute"},
            tool_already_in_flight=True,
            argument_pivot=False,
            user_intent_match=False,
        )
        decision, reasons = eval_policy(pi)
        assert decision == "deny"

    def test_argument_pivot_forces_the_gate_even_at_write_tier(self):
        """A write-tier tool already in flight is normally exempt (see
        above) -- UNLESS its target argument diverges from trusted prior
        context (argument_pivot=True), e.g. a second send-type call to a
        different, unrecognized recipient. This should force the gate
        despite the tool having been 'legitimately' used already."""
        pi = base_input(
            injection_surface="tool_output",
            proposed_tool={"name": "update_scheduled_transaction", "sensitivity_tier": "write"},
            tool_already_in_flight=True,
            argument_pivot=True,
            user_intent_match=False,
        )
        decision, reasons = eval_policy(pi)
        assert decision == "deny"


# ===========================================================================
# SECTION 2 -- known gaps (fail-open). These document CURRENT behavior, not
# desired behavior. See conversation history for the production-readiness
# review these came from. If someone fixes the fail-open issue, these tests
# SHOULD start failing -- that's the point, it forces a deliberate update.
# ===========================================================================

class TestKnownFailOpenGaps:
    def test_KNOWN_GAP_empty_input_defaults_to_allow(self):
        """Completely empty input -> allow. No error, no signal that
        anything was even missing."""
        decision, reasons = eval_policy({})
        assert decision == "allow"  # documents current behavior, not endorsement

    def test_KNOWN_GAP_typo_in_sensitivity_tier_silently_bypasses_the_gate(self):
        """'IRREVERSIBLE' (wrong case) vs 'irreversible' -- the tier_rank
        lookup fails silently (undefined, not an error), the tier check
        never fires, and an otherwise-classic malicious pivot is allowed
        straight through. This is the most dangerous of the three gaps:
        a config/data typo produces a SILENT security bypass, not a crash."""
        pi = base_input(
            injection_surface="tool_output",
            proposed_tool={"name": "send_money", "sensitivity_tier": "IRREVERSIBLE"},
            tool_already_in_flight=False,
            user_intent_match=False,
        )
        decision, reasons = eval_policy(pi)
        assert decision == "allow"  # should be "deny" -- this is the bug

    def test_KNOWN_GAP_missing_user_intent_match_field_defaults_to_allow(self):
        """If the harness ever forgets to set user_intent_match (a None,
        a dropped key, a serialization bug), BOTH the deny-branch and the
        escalate-branch fail to derive (comparing undefined to true/false
        is undefined, not false), and the entire pivot gate goes dark."""
        pi = base_input(
            injection_surface="tool_output",
            proposed_tool={"name": "send_money", "sensitivity_tier": "irreversible"},
            tool_already_in_flight=False,
        )
        del pi["user_intent_match"]
        decision, reasons = eval_policy(pi)
        assert decision == "allow"  # should be "deny" -- this is the bug

    def test_KNOWN_GAP_missing_proposed_tool_defaults_to_allow(self):
        """A missing proposed_tool object entirely -- chained field access
        (input.proposed_tool.sensitivity_tier) is undefined, so the tier
        check never fires."""
        pi = base_input(injection_surface="tool_output", user_intent_match=False)
        del pi["proposed_tool"]
        decision, reasons = eval_policy(pi)
        assert decision == "allow"  # should be "deny" -- this is the bug


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# ===========================================================================
# SECTION 3 -- a-priori principal-impersonation rule.
# See the caveat block in intent_scanner.py: this rule is specified on
# principle, NOT validated on data (the pattern is absent from the AIB dev
# split and from all 455 AgentDojo traces). These tests assert the rule's
# LOGIC is correct and, critically, that it is inert when the field is
# absent -- which is what keeps every reported metric unaffected by it.
# ===========================================================================

class TestPrincipalImpersonation:
    def test_claimed_user_intent_denies_even_at_read_tier(self):
        """The rule is tier-independent by design: fabricated authorization is
        evidence of injection regardless of what the tool does, and read-tier
        tools are otherwise never gated at all."""
        pi = base_input(
            injection_surface="tool_output",
            proposed_tool={"name": "web_search", "sensitivity_tier": "read"},
            user_intent_match=True,
            untrusted_claims_user_intent=True,
        )
        decision, reasons = eval_policy(pi)
        assert decision == "deny"
        assert "untrusted_content_impersonating_user_principal" in reasons

    def test_claimed_user_intent_cannot_be_softened_to_escalate(self):
        """The attack's whole purpose is to manufacture the user_intent_match
        that would turn a deny into an escalate. Assert it cannot."""
        pi = base_input(
            injection_surface="tool_output",
            proposed_tool={"name": "send_money", "sensitivity_tier": "irreversible"},
            tool_already_in_flight=False,
            user_intent_match=True,
            untrusted_claims_user_intent=True,
        )
        decision, _ = eval_policy(pi)
        assert decision == "deny", "impersonated intent must not reach the escalate branch"

    def test_rule_is_inert_on_trusted_surface(self):
        """A trusted first-turn request is not untrusted content, so the rule
        must not fire even if the flag is somehow set."""
        pi = base_input(
            injection_surface="direct_user_request",
            proposed_tool={"name": "get_balance", "sensitivity_tier": "read"},
            untrusted_claims_user_intent=True,
        )
        decision, _ = eval_policy(pi)
        assert decision == "allow"

    def test_absent_field_leaves_all_other_decisions_unchanged(self):
        """Backward compatibility: harnesses that never set this field must
        behave exactly as before. This is what guarantees the reported
        detection/FPR numbers are unaffected by the rule's existence."""
        for overrides, expected in [
            ({}, "allow"),
            ({"injection_surface": "tool_output",
              "proposed_tool": {"name": "send_money", "sensitivity_tier": "irreversible"},
              "user_intent_match": False}, "deny"),
            ({"injection_surface": "tool_output",
              "proposed_tool": {"name": "update_user_info", "sensitivity_tier": "write"},
              "user_intent_match": True}, "escalate"),
        ]:
            pi = base_input(**overrides)
            assert "untrusted_claims_user_intent" not in pi
            decision, _ = eval_policy(pi)
            assert decision == expected, f"{overrides} -> {decision}, expected {expected}"
