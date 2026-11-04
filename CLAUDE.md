# Tisma Cloud: notes for Claude

The proprietary server of Tisma (master, nodes, API, administration, deploy). Private repository; it was `server/` of
the public Tisma Slicer repository until 2026-11-04. It runs Tisma Slicer, tisma-engine and tisma_scan as separate
programs and contains no code of them (they are AGPL; see `THIRD_PARTY_LICENSES.md`).

- Answer the owner in Spanish. Code, comments, commit messages and PR titles in English.
- No model identifiers in code, commits or PR text (only the attribution trailer the session asks for).
- Audit before changing; build and test before reporting; say plainly what was not tested.
- Keep the line endings of the files you edit.
- State lives in `HANDOFF.md`, not in the conversation.
- The engine's contract is `docs/spec/features/cloud.md` of `adriangrochi-code/tisma_engine` (read only). The scanner
  side is `adriangrochi-code/openscanner` (`tools/tisma_scan.py`, the app's `CloudClient.kt`).

Tests: `pip install -e '.[master,test]'` and `python -m pytest -q` (SQLite; `TISMA_TEST_DATABASE_URL` for PostgreSQL).
