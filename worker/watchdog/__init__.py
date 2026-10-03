"""The watchdog (Stage 6, PLAN.md §11): every five minutes it checks the system against fixed
signatures, opens and resolves incidents, and tells Telegram and the Paperclip crew.

- checks.py: the signatures, as plain functions of a snapshot of the system.
- reconcile.py: how one pass's findings move the incidents.
- paperclip.py: the Incident routine's webhook and the Paperclip health probe.
- run.py: one pass, end to end.

The rows are worker/db/incidents.py's and worker/db/watchdog.py's.
"""
