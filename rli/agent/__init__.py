"""System C — the LLM investigator plus the deterministic controller (spec.md §2/§4).

The responsibility split spec.md §2 mandates is the package layout:
`investigator` proposes (schema-validated model output), `controller`
disposes (eligibility, permissions, budgets, ranking, hard stops), `loop`
drives, `explanation` cites. Nothing outside `investigator` and
`explanation` talks to an LLM.
"""
