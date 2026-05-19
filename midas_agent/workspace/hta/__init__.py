"""HTA — Hypothesis-Tree Agent runtime mode.

A two-layer agent architecture: an outer runtime-constructed decision graph
(non-DAG, backward edges allowed) and inner ReAct loops inside execution nodes.
At each decision point the agent generates G mutually exclusive hypotheses,
verifies them, picks a winner by group-relative advantage, and records all
advantages in a typed memory keyed by (decision_type, hypothesis_class).
"""
