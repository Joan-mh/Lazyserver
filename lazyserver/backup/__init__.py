"""Backup layer (architecture §4).

Phase 3 only needs the SHA-256 helper for change detection on editor
return (FR-2.1 modification detection). The full backup store + pending
set come in Phase 4.
"""
