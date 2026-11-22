"""Shared LLMOps platform used by every service in the agency.

Every topic service (whatever agent framework it uses) gets the same
cross-cutting behaviour from here: auth, subscription entitlements, quotas,
model-gateway access (tiered model routing), tracking, and service scaffolding.
"""
