"""Minimal compatibility shim for outlines airport imports.

The published pyairports==0.0.1 wheel currently does not include the
`pyairports` module files that outlines expects. vLLM imports outlines at
startup, so provide an empty airport list to keep server startup working for
our workloads, which do not use the airport grammar types.
"""

AIRPORT_LIST = []
