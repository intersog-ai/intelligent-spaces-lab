"""islab.approval (B07): the agent and the approval gate.

Builds on islab.policy without editing it. Public API:

  GatedEngine          B01 PolicyEngine + bound, signed, single-use, expiring approvals
  ApprovalLedger       tickets: issue / verify a reply / check right before execution
  StandingRule(s)      approvals set in advance, with limits (days, time window, uses per day,
                       longest window, only for deliveries the household entered)
  Phone, PersonModel   phone double (signs what it shows) and a person model with fatigue
  B07Agent, B07Planner, InjectionProneLLM   agent loop and agent doubles (no real LLM)
  add_b07_tools, b07_policy, describe, word_args, b07_intents
  Spy, check           independent level-3 checker
"""
from .tools import (add_b07_tools, b07_policy, describe, word_args, b07_intents, b07_intent_tools, HIGH_RISK,
                    PURPOSES, default_until, until_abs, s_to_hhmm, hhmm_to_s)
from .ledger import ApprovalLedger, Ticket, action_digest, sign, make_keys
from .standing import StandingRule, StandingRules
from .engine import GatedEngine
from .phone import Phone, PersonModel
from .agent import B07Agent, B07Planner, InjectionProneLLM
from .checker import Spy, check

__all__ = ["GatedEngine", "ApprovalLedger", "Ticket", "action_digest", "sign", "make_keys", "StandingRule",
           "StandingRules", "Phone", "PersonModel", "B07Agent", "B07Planner", "InjectionProneLLM",
           "add_b07_tools", "b07_policy", "describe", "word_args", "b07_intents", "b07_intent_tools",
           "HIGH_RISK", "PURPOSES", "default_until", "until_abs", "s_to_hhmm", "hhmm_to_s", "Spy", "check"]
