"""Problem reports sent from Tisma on the computer and from the phone app, and their catalog for the programmers (and the
AI that programs with them): the reports of the same problem grouped, the most frequent first, with what is needed to
reproduce it and nothing personal.

- redact(): what could identify a person is removed from the texts and the log before they are kept (e-mails,
  addresses, the user folder in paths, tokens, keys, access codes and serial numbers).
- signature(): reports of the same problem share it (the app, the first error line of the log without numbers or
  paths, else the words of the title).
- catalog(): the groups, as JSON lines (one group per line) or Markdown, with their kind of error, key words and a
  summary (triage.py) and the texts translated to the language of the team when the translator is on (translate.py).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import shlex
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from . import triage

APPS = ("pc", "android", "web")
KINDS = ("bug", "crash", "suggestion")
STATUSES = ("new", "confirmed", "fixed", "wontfix", "duplicate")

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# IPv6 candidates (checked with ipaddress: a time as 18:24:53 is not one).
_IPV6 = re.compile(r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])")
_MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
# The user folder of the system in a path (Windows, Linux, macOS).
_HOME = re.compile(r"(?i)([A-Z]:[\\/]+Users[\\/]+|/home/|/Users/)([^\\/\s\"']+)")
_BEARER = re.compile(r"(?i)\bBearer\s+\S+")
_TOKEN = re.compile(r"\b(?:tu|tn|tb)_[A-Za-z0-9_-]{16,}")
# key = value of secrets (also "access code: 12345678" of the Bambu Lab printers and their serial numbers).
_SECRET = re.compile(r"(?i)\b(access[ _-]?code|password|passwd|pass|api[ _-]?key|apikey|token|secret|serial(?:[ _-]?number)?|"
                     r"sn|authorization)(\"?\s*[:=]\s*\"?)([^\s\"',;&]+)")
_LONG_HEX = re.compile(r"\b[0-9A-Fa-f]{32,}\b")

# The log a report may bring, the part of it that is kept, and the part of it in the catalog [characters].
LOG_MAX = 200_000
LOG_KEPT = 20_000
EXCERPT_MAX = 8_000
TITLE_MAX = 200
TEXT_MAX = 10_000


def _ipv6(m: re.Match) -> str:
    try:
        ipaddress.IPv6Address(m.group(0))
    except ValueError:
        return m.group(0)
    return "<ip>"


def redact(text: str) -> str:
    """The text without what could identify the person or open their accounts and printers."""
    if not text:
        return ""
    # Bearer before the key = value: "Authorization: Bearer <token>" is hidden whole.
    text = _BEARER.sub("Bearer <token>", text)
    text = _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}<hidden>", text)
    text = _TOKEN.sub("<token>", text)
    text = _EMAIL.sub("<email>", text)
    text = _MAC.sub("<mac>", text)
    text = _IPV6.sub(_ipv6, text)
    text = _IPV4.sub("<ip>", text)
    text = _HOME.sub(lambda m: m.group(1) + "<user>", text)
    return _LONG_HEX.sub("<hex>", text)


_ERROR_LINE = re.compile(r"(?i)\b(error|exception|assert|terminate called|segmentation fault|fatal|crash|traceback|"
                         r"unhandled|access violation|sigsegv|sigabrt)\b")
_NOISE = re.compile(r"<[a-z]+>|0x[0-9a-fA-F]+|\d+([.,:]\d+)*|[A-Za-z]:\\[^\s]*|/[^\s]*")
_WORD = re.compile(r"[a-záéíóúñü]{3,}")


def first_error(log: str) -> str:
    for line in log.splitlines():
        if _ERROR_LINE.search(line):
            return line.strip()
    return ""


def _normalized(line: str) -> str:
    """A log line without what changes between two runs (numbers, addresses, paths, time stamps)."""
    line = re.sub(r"^\[[^\]]*\]\s*", "", line)          # a time stamp in brackets
    return re.sub(r"\s+", " ", _NOISE.sub("#", line)).strip().lower()


def signature(app: str, title: str, log: str) -> str:
    error = first_error(log)
    if error:
        key = "log:" + _normalized(error)
    else:
        words = sorted(set(_WORD.findall(title.lower())))
        key = "title:" + " ".join(words)
    return hashlib.sha256(f"{app}|{key}".encode()).hexdigest()[:16]


def excerpt(log: str, limit: int = EXCERPT_MAX) -> str:
    """The error lines with two lines around them, and the end of the log, up to limit characters."""
    lines = log.splitlines()
    keep: set[int] = set()
    for i, line in enumerate(lines):
        if _ERROR_LINE.search(line):
            keep.update(range(max(0, i - 2), min(len(lines), i + 3)))
    keep.update(range(max(0, len(lines) - 40), len(lines)))
    out, last = [], -1
    for i in sorted(keep):
        if last >= 0 and i != last + 1:
            out.append("...")
        out.append(lines[i])
        last = i
    text = "\n".join(out)
    return text if len(text) <= limit else "...\n" + text[-limit:]


def clean(app: str, kind: str, title: str, description: str = "", steps: str = "", expected: str = "", actual: str = "",
          app_version: str = "", os: str = "", log: str = "") -> dict:
    """The fields of a report as they are kept: without anything personal (redacted before they are cut, so that no
    secret is cut in two and left), the log reduced to its errors and its end, and the signature of the problem."""
    log = redact(log[:LOG_MAX])
    title = redact(title)[:TITLE_MAX].strip()
    return {
        "app": app, "kind": kind, "title": title,
        "description": redact(description)[:TEXT_MAX].strip(), "steps": redact(steps)[:TEXT_MAX].strip(),
        "expected": redact(expected)[:TEXT_MAX].strip(), "actual": redact(actual)[:TEXT_MAX].strip(),
        "app_version": redact(app_version)[:40].strip(), "os": redact(os)[:80].strip(),
        "log": excerpt(log, LOG_KEPT) if log.strip() else "",
        "signature": signature(app, title, log),
        "category": triage.category(kind, title, "\n".join((description, steps, expected, actual)), log),
    }


TEXT_FIELDS = ("title", "description", "steps", "expected", "actual")


def translation_of(r) -> dict:  # noqa: ANN001  (a BugReport)
    try:
        value = json.loads(r.translation or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def texts_of(r) -> dict:  # noqa: ANN001
    """The texts of a report in the language of the team when they were translated, else as they were written."""
    translated = translation_of(r)
    return {k: translated.get(k) or getattr(r, k) or "" for k in TEXT_FIELDS}


def corpus_of(reports) -> triage.Corpus:  # noqa: ANN001
    return triage.Corpus([" ".join(texts_of(r).values()) for r in reports])


def error_text(line: str) -> str:
    """A line of the log without its time, thread and level in brackets."""
    return re.sub(r"^(\[[^\]]*\]\s*)+", "", line).strip()


def digest(r, corpus: triage.Corpus) -> dict:  # noqa: ANN001
    """What the team reads first of a report: its summary and its key words."""
    texts = texts_of(r)
    body = " ".join(texts[k] for k in ("description", "steps", "expected", "actual"))
    # A translation of a title can miss the words of 3D printing: the title as written goes with it.
    title = texts["title"] if texts["title"] == r.title else f"{texts['title']} ({r.title})"
    return {
        "summary": triage.summary(r.category or "other", title,
                                  [texts["description"], texts["actual"], texts["steps"]], corpus,
                                  error_text(first_error(r.log or "")), triage.printer_of(r.log or "")),
        "keywords": triage.keywords(texts["title"] + " " + body, corpus),
    }


# The fields of the search ("impresora:kobra", "version:0.9"), in Spanish and English, without accents.
SEARCH_FIELDS = {
    "impresora": "printer", "printer": "printer", "version": "version", "sistema": "os", "system": "os", "os": "os",
    "idioma": "language", "language": "language", "error": "error", "titulo": "title", "title": "title", "app": "app",
    "tipo": "category", "type": "category", "category": "category", "categoria": "category",
}


def parse_query(query: str) -> tuple[dict[str, list[str]], list[str]]:
    """The words of a search for given fields ("impresora:kobra", "impresora:\"kobra 3\"") and the free ones, without
    case or accents."""
    try:
        parts = shlex.split(query)
    except ValueError:        # an open quote
        parts = query.replace('"', "").split()
    fields: dict[str, list[str]] = {}
    free: list[str] = []
    for part in parts:
        name, sep, value = part.partition(":")
        key = SEARCH_FIELDS.get(triage.normalize(name)) if sep and value else None
        if key:
            fields.setdefault(key, []).append(triage.normalize(value))
        else:
            free += triage.normalize(part).split()
    return fields, free


def field_text(r, key: str) -> str:  # noqa: ANN001
    """The value of a field of the search in a report, without case or accents."""
    if key == "printer":
        value = triage.printer_of(r.log or "") + " " + " ".join(re.findall(r"(?m)^Printer: (.*)$", r.log or ""))
    elif key == "version":
        value = r.app_version or ""
    elif key == "os":
        value = r.os or ""
    elif key == "language":
        value = r.language or ""
    elif key == "error":
        value = first_error(r.log or "")
    elif key == "title":
        value = f"{r.title} {translation_of(r).get('title', '')}"
    elif key == "app":
        value = r.app or ""
    else:   # category: its key or its name
        value = f"{r.category} {triage.CATEGORY_NAMES.get(r.category, '')}"
    return triage.normalize(value)


def matches(r, query: str) -> bool:  # noqa: ANN001
    """The report has every word of the query: those of a field in that field, the free ones anywhere in its texts, as
    written or translated (any case and accents)."""
    fields, free = parse_query(query)
    for key, values in fields.items():
        text = field_text(r, key)
        if not all(v in text for v in values):
            return False
    if not free:
        return True
    text = triage.normalize(" ".join([*(getattr(r, k) or "" for k in TEXT_FIELDS), *translation_of(r).values(),
                                      first_error(r.log or ""), r.app_version or "", r.os or "",
                                      triage.printer_of(r.log or "")]))
    return all(w in text for w in free)


def system_of(os_text: str) -> str:
    """The family of a system ("Windows 11 Pro" -> "Windows") for the filters."""
    os_text = (os_text or "").strip()
    for name in ("Windows", "macOS", "Android", "iOS", "Linux"):
        if name.lower() in os_text.lower() or (name == "macOS" and "mac os" in os_text.lower()):
            return name
    return os_text.split(" ")[0] if os_text else ""


@dataclass
class Group:
    signature: str
    app: str
    title: str
    count: int = 0
    kinds: set[str] = field(default_factory=set)
    versions: set[str] = field(default_factory=set)
    systems: set[str] = field(default_factory=set)
    statuses: dict[str, int] = field(default_factory=dict)
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    error: str = ""
    examples: list[dict] = field(default_factory=list)
    log_excerpt: str = ""
    ids: list[str] = field(default_factory=list)
    # Reports sent with an account (their authors can be rewarded) and the ones rewarded.
    with_account: int = 0
    rewarded: int = 0
    # The kind of error most of its reports have, the languages they were written in, what the team reads first.
    categories: Counter = field(default_factory=Counter)
    languages: set[str] = field(default_factory=set)
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    translated: list[dict] = field(default_factory=list)

    @property
    def category(self) -> str:
        return self.categories.most_common(1)[0][0] if self.categories else "other"

    def as_dict(self) -> dict:
        return {
            "signature": self.signature, "app": self.app, "title": self.title, "reports": self.count,
            "kinds": sorted(self.kinds), "versions": sorted(self.versions), "systems": sorted(self.systems),
            "statuses": self.statuses,
            "first_seen": self.first_seen.isoformat(timespec="seconds") + "Z" if self.first_seen else None,
            "last_seen": self.last_seen.isoformat(timespec="seconds") + "Z" if self.last_seen else None,
            "first_error": self.error, "examples": self.examples, "log_excerpt": self.log_excerpt,
            "report_ids": self.ids, "with_account": self.with_account, "rewarded": self.rewarded,
            "category": self.category, "category_name": triage.CATEGORY_NAMES.get(self.category, self.category),
            "languages": sorted(self.languages), "summary": self.summary, "keywords": self.keywords,
            "translated_examples": self.translated,
        }


def catalog(reports, max_examples: int = 3, corpus: triage.Corpus | None = None) -> list[Group]:  # noqa: ANN001
    """The reports (BugReport rows) grouped by signature, the most frequent (then the most recent) first. The words are
    weighed among these reports, or among those of corpus."""
    reports = sorted(reports, key=lambda r: r.created_at)
    corpus = corpus or corpus_of(reports)
    groups: dict[str, Group] = {}
    best: dict[str, tuple[int, object]] = {}
    for r in reports:
        g = groups.get(r.signature)
        if g is None:
            g = groups[r.signature] = Group(r.signature, r.app, r.title, first_seen=r.created_at)
        g.count += 1
        g.kinds.add(r.kind)
        if r.app_version:
            g.versions.add(r.app_version)
        if r.os:
            g.systems.add(r.os)
        g.statuses[r.status] = g.statuses.get(r.status, 0) + 1
        g.last_seen = r.created_at
        g.ids.append(r.id)
        g.with_account += r.user_id is not None
        g.rewarded += r.rewarded_at is not None
        if not g.error and r.log:
            g.error = first_error(r.log)
        if r.log:
            g.log_excerpt = excerpt(r.log)
        g.categories[r.category or "other"] += 1
        if r.language:
            g.languages.add(r.language)
        if len(g.examples) < max_examples:
            g.examples.append({k: v for k, v in (("description", r.description), ("steps", r.steps),
                                                  ("expected", r.expected), ("actual", r.actual)) if v})
            translated = {k: v for k, v in translation_of(r).items() if v and k != "title"}
            if translated:
                g.translated.append(translated)
        # The summary of the group is the one of its best explained report (the longest texts).
        size = sum(len(v) for v in texts_of(r).values())
        if r.signature not in best or size > best[r.signature][0]:
            best[r.signature] = (size, r)
    for sig, g in groups.items():
        d = digest(best[sig][1], corpus)
        g.summary, g.keywords = d["summary"], d["keywords"]
    return sorted(groups.values(), key=lambda g: (-g.count, -(g.last_seen.timestamp() if g.last_seen else 0)))


def to_jsonl(groups: list[Group]) -> str:
    return "".join(json.dumps(g.as_dict(), ensure_ascii=False) + "\n" for g in groups)


def to_markdown(groups: list[Group]) -> str:
    """The catalog to give to a programmer or an AI: one section per problem, what to reproduce and the evidence."""
    out = ["# Tisma: problems reported by the users", "",
           "Each section is one problem (reports grouped by the first error of the log, else by the words of the "
           "title), the most reported first. Personal data was removed from the texts and the logs before they were "
           "kept. Reproduce with the steps, fix the cause, add a test.", ""]
    for i, g in enumerate(groups, 1):
        out += [f"## {i}. {g.title} ({g.app}, {g.count} report{'s' if g.count != 1 else ''})", "",
                f"- Signature: `{g.signature}`",
                f"- Kind: {', '.join(sorted(g.kinds))}; status: "
                + ", ".join(f"{k} {v}" for k, v in sorted(g.statuses.items())),
                f"- Versions: {', '.join(sorted(g.versions)) or '-'}; systems: {', '.join(sorted(g.systems)) or '-'}",
                f"- Seen: {g.first_seen:%Y-%m-%d} to {g.last_seen:%Y-%m-%d}" if g.first_seen and g.last_seen else "- Seen: -",
                f"- Kind of error: {triage.CATEGORY_NAMES.get(g.category, g.category)}; key words: "
                f"{', '.join(g.keywords) or '-'}; languages: {', '.join(sorted(g.languages)) or '-'}",
                f"- Summary: {g.summary}"]
        if g.error:
            out.append(f"- First error: `{g.error}`")
        for n, ex in enumerate(g.examples, 1):
            out += ["", f"### Report {n}"]
            for key, label in (("description", "What happened"), ("steps", "Steps"), ("expected", "Expected"),
                               ("actual", "Actual")):
                if ex.get(key):
                    out += [f"**{label}:**", "", ex[key].strip(), ""]
        for n, ex in enumerate(g.translated, 1):
            out += ["", f"### Report {n}, translated"]
            for key, label in (("description", "What happened"), ("steps", "Steps"), ("expected", "Expected"),
                               ("actual", "Actual")):
                if ex.get(key):
                    out += [f"**{label}:**", "", ex[key].strip(), ""]
        if g.log_excerpt:
            out += ["", "### Log (excerpt)", "", "```", g.log_excerpt, "```"]
        out.append("")
    return "\n".join(out)
