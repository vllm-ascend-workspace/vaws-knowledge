"""Fault stand-in: importing jsonschema never completes validation.

Used only to prove the gate adapter does not treat a dependency crash as
a semantic reject. PYTHONPATH points here for that one subprocess.
"""

raise RuntimeError("dependency initialization crash")
