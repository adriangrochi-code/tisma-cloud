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

## Code style: «Marlin style» (owner, 2026-11-23)

New code, and code being touched, is written and documented in the style of Marlin's firmware, in as much detail as
needed for anyone to understand it by reading it. Existing code is not rewritten all at once. Code comments and
documentation are in English (the owner's choice for this repository); texts a person sees (window, panel, messages
of the service) stay in Spanish.

1. A header in each file: what it is, what it is for, how it fits with the rest, units and conventions, an example.
2. Sections with large separators (`# ===== Name =====`) and subsections (`# --- name ---`).
3. Each configuration setting explained where it is defined (as in Configuration.h): what it does, unit, sensible
   range, default, and what happens when it is overdone.
4. Each function and class documented: what it does, parameters with units, what it returns, errors, and why it is
   done this way when that is not obvious.
5. Inline comments for what is not evident (formulas, performance shortcuts, compatibility, security); not for the
   obvious.
6. Descriptive names, no cryptic abbreviations.
