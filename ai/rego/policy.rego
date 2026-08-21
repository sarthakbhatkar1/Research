package agent.guardrail

import rego.v1

# ---------------------------------------------------------------------------
# AgentInjectionBench action-layer policy
#
# This policy makes NO attempt to read the injected text. It only consumes
# structural facts about (a) which channel is currently driving the agent's
# next action and (b) what that action would be. The premise: on the
# benchmark's own leaderboard, content-level scanners top out at 73.6%
# balanced accuracy and 35% of attacks (concentrated on the tool_output
# surface) evade every scanner unanimously. An action-layer policy is meant
# to catch a disjoint slice of that residual set -- cases where the injected
# TEXT is invisible to a scanner but the resulting ACTION is still anomalous
# (wrong tool tier for what the user actually asked, or a poisoned tool
# definition) -- not to replace the scanners.
#
# Input contract (see harness.py:build_policy_input for how each field is
# derived, and its honesty caveats):
#
#   injection_surface       : which channel supplied the content currently
#                              proposing the next action.
#   proposed_tool.name       : the tool under consideration for the next call.
#   proposed_tool.sensitivity_tier : "read" | "write" | "execute" | "irreversible"
#   tool_already_in_flight   : bool -- has THIS tool already been called
#                              earlier in the session (a legitimate,
#                              cheaply-observable session fact -- not a text
#                              read). A pivot to a tool that was never part
#                              of the session so far, proposed by untrusted
#                              content, is the single strongest structural
#                              tell in this benchmark: 129/133 pivot cases are
#                              attacks vs. 13/49 same-tool-continuation cases.
#                              NOTE: this exemption is deliberately NOT
#                              granted at execute/irreversible tier -- see
#                              continuation_exempt below. A "legitimize the
#                              tool early, weaponize it later" multi-turn
#                              attack defeats a same-tool-continuation
#                              exemption at exactly the tiers where a repeat
#                              call does the most damage.
#   argument_pivot            : bool -- for a REPEAT call to an
#                              already-in-flight tool, does this call's
#                              target-identifying argument (e.g. a
#                              recipient account) diverge from every value
#                              seen in trusted prior context this session.
#                              Real signal where the harness can compute it
#                              (AgentDojo's send_money against known
#                              transaction-history recipients); defaults to
#                              false where the source data has no structured
#                              args to compare (AgentInjectionBench).
#   user_intent_match        : bool -- secondary/softer signal: does the
#                              user's own message plausibly call for this
#                              tool/action (token-overlap heuristic). Used
#                              only to decide deny vs. confirm once a pivot
#                              has already triggered the gate -- not to
#                              excuse a pivot on its own, since it is a much
#                              weaker (noisier) signal than tool_already_in_flight.
#   definition_layer_flag    : bool -- Layer-1 (tool_definition_scanner)
#                              verdict on tools_available[].description.
# ---------------------------------------------------------------------------

untrusted_surfaces := {
	"tool_output",
	"rag_document",
	"file_content",
	"api_response",
	"mcp_response",
	"user_message",
}

tier_rank := {"read": 0, "write": 1, "execute": 2, "irreversible": 3}

gate_threshold := tier_rank.write

is_untrusted if input.injection_surface in untrusted_surfaces

tier_at_or_above_gate if {
	rank := tier_rank[input.proposed_tool.sensitivity_tier]
	rank >= gate_threshold
}

# --- individual denial/confirmation reasons (kept separate for auditability) --

deny_poisoned_definition if input.definition_layer_flag == true

high_risk_tier if tier_rank[input.proposed_tool.sensitivity_tier] >= tier_rank.execute

# A repeat call to an already-in-flight tool is exempt from the pivot gate
# ONLY at write tier, and ONLY when its arguments don't diverge from trusted
# prior context. At execute/irreversible tier, or whenever argument_pivot
# fires, "already used this tool" does not excuse this specific call --
# closing the "legitimize early, weaponize later" multi-turn evasion found
# in AgentInjectionBench's multi_turn_stateful category.
continuation_exempt if {
	input.tool_already_in_flight == true
	not high_risk_tier
	object.get(input, "argument_pivot", false) != true
}

# Primary gate: an untrusted surface is steering the agent toward a
# higher-tier tool that isn't covered by the continuation exemption above.
untrusted_pivot if {
	is_untrusted
	tier_at_or_above_gate
	not continuation_exempt
}

deny_pivot_no_intent_match if {
	untrusted_pivot
	input.user_intent_match == false
}

confirm_pivot_with_intent_match if {
	untrusted_pivot
	input.user_intent_match == true
}

# --- top-level decision -------------------------------------------------

default decision := "allow"

decision := "deny" if deny_poisoned_definition

decision := "deny" if {
	not deny_poisoned_definition
	deny_pivot_no_intent_match
}

decision := "confirm" if {
	not deny_poisoned_definition
	not deny_pivot_no_intent_match
	confirm_pivot_with_intent_match
}

# --- explanation: why the decision was reached (for the audit log) ------

reasons contains "poisoned_tool_definition" if deny_poisoned_definition

reasons contains "untrusted_source_tool_pivot_no_intent_match" if deny_pivot_no_intent_match

reasons contains "untrusted_source_tool_pivot_intent_match_needs_confirmation" if confirm_pivot_with_intent_match

reasons contains "read_tier_or_same_tool_or_trusted_source" if {
	not deny_poisoned_definition
	not deny_pivot_no_intent_match
	not confirm_pivot_with_intent_match
}
