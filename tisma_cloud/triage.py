"""What the team reads first of a problem report, made on the master without any outside service: its kind of error
(by words of Spanish, English and Portuguese), its key words (the most distinctive ones among all the reports) and a
short summary (the sentences that say most, the first error of the log and the printer).

- category(): the kind of error of a report (CATEGORIES).
- Corpus: the frequency of the words in the reports, to weigh them (a word every report has says nothing).
- keywords(), summary().
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter

# (key, name for the team, starts of the words that tell it). A start of 4 letters or less is a whole word (or its
# plural): "plan" is not "planchado". Normalized: lower case, without accents.
CATEGORIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("crash", "Cierre o cuelgue",
     ("crash", "cierr", "cerro", "cerr", "colg", "cuelg", "fecho", "fechou", "segfault", "segmentation", "sigsegv",
      "sigabrt", "terminate", "freez", "congel", "travou", "trava", "hang", "hangs")),
    ("slicing", "Laminado",
     ("slic", "lamin", "fatia", "gcode", "g-code", "toolpath", "trayector", "perimet", "relleno", "infill",
      "preench", "capa", "layer", "camada", "seam", "costura", "brim", "borde", "skirt", "falda", "arco", "arcs",
      "puente", "bridge", "ponte", "overhang", "voladiz", "retrac", "ironing", "planch", "planar")),
    ("supports", "Soportes", ("support", "soport", "suport", "tree", "arbol", "arvore", "raft", "balsa")),
    ("printer", "Impresora y conexión",
     ("printer", "impresor", "impressor", "connect", "conect", "conex", "usb", "wifi", "wi-fi", "network", "lan",
      "bambu", "octoprint", "moonraker", "klipper", "mqtt", "ftp", "prusalink", "send", "enviar", "upload", "firmware",
      "marlin", "serial", "baud")),
    ("camera", "Cámara", ("camera", "camara", "webcam", "video", "obico", "stream")),
    ("calibration", "Calibración", ("calibr", "torre", "tower", "flujo", "vibrac", "shaper", "shaping")),
    ("resin", "Resina", ("resin", "resina", "sla", "msla", "exposure", "exposic", "exposi", "goo", "ctb", "lcd")),
    ("engineering", "Ingeniería",
     ("fea", "engineer", "ingenier", "engenhar", "estructur", "structur", "estrutur", "stress", "tension", "tensao",
      "deformac", "aerodin")),
    ("cloud", "Cuenta y nube",
     ("cloud", "nube", "nuvem", "account", "cuenta", "conta", "login", "password", "contrasen", "senha", "plan",
      "pago", "payment", "pagamento", "subscription", "suscrip", "assinatura", "remot")),
    ("view", "Vista 3D",
     ("render", "opengl", "gpu", "graphic", "grafic", "visualiz", "preview", "previsual", "shader", "textur",
      "sombra", "shadow")),
    ("interface", "Interfaz",
     ("button", "boton", "botao", "window", "ventana", "janela", "panel", "painel", "menu", "dialog", "screen",
      "pantalla", "tela", "translat", "traduc", "idioma", "language", "theme", "tema", "font", "fuente", "fonte",
      "icon", "icono", "click", "clic", "scroll", "layout")),
    ("profiles", "Perfiles y ajustes",
     ("profile", "perfil", "preset", "filament", "nozzle", "boquilla", "bico", "bed", "cama", "mesa", "temperat",
      "setting", "ajuste", "configur")),
    ("performance", "Rendimiento", ("slow", "lento", "lenta", "memory", "memoria", "cpu", "tarda", "demora")),
    ("other", "Otro", ()),
)
CATEGORY_NAMES = {key: name for key, name, _ in CATEGORIES}
# Categories that most reports touch in passing (who reports a missing support also sliced): their words count less, so
# that the specific one wins.
_GENERAL = {"slicing": 0.5, "profiles": 0.7, "interface": 0.8}

# Words that say nothing of the problem (Spanish, English, Portuguese; without accents) and the marks of what redact()
# removed.
STOPWORDS = frozenset("""
que de la el en y a los las un una unos unas por con para del al se no es lo como mas pero sus le ya o fue este ha si
porque esta son entre cuando muy sin sobre tambien me hasta hay donde quien desde todo nos durante todos uno les ni
contra otros ese eso ante ellos e esto mi antes algunos yo otro otras otra tanto esa estos mucho quienes nada muchos
cual poco ella estar estas algunas algo nosotros mis tu te ti tus ellas era eran estaba estaban tengo tiene tienen
tenia hace hacer hago puede pueden puedo despues luego entonces aqui ahi alli cada vez veces cuando sale salio hola
gracias favor deberia deberian queda quedo pasa paso bajo falta faltan aparece aparecen funciona anda usar uso
ultima ultimo quiero quisiera siempre nunca solo estan ella ello
the a an and or but if then of to in on at by for with from as is are was were be been being it its this that these
those i you he she we they me my your our their not no do does did have has had can could should would will just so
than too very there here what which who when where why how all any each more most other some such only own same into
over under again further once about after before also get got please hello thanks dont doesnt didnt cant cannot
o os as um uma de do da dos das em no na nos nas por para com sem que e ou mas se nao sim ao aos ele ela eles elas eu
voce meu minha seu sua isso isto esse essa este esta foi ser tem ter faz fazer pode quando depois antes muito mais
menos tambem ja obrigado ola
hidden email user token hex mac ip tisma slicer error
""".split())

_TOKEN = re.compile(r"[a-z][a-z0-9+\-]*[a-z0-9]|[a-z]")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_PRINTER = re.compile(r"(?m)^Printer: ([^\n,]+)")


def normalize(text: str) -> str:
    """Lower case without accents: "Cámara" and "camara" are the same word."""
    text = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def words(text: str) -> list[str]:
    return _TOKEN.findall(normalize(text))


def terms(text: str) -> list[str]:
    """The words that may say something of the problem: not the common ones, nor numbers."""
    return [w for w in words(text) if len(w) >= 3 and w not in STOPWORDS and not w[0].isdigit()]


def _matches(word: str, start: str) -> bool:
    if len(start) <= 4:
        return word in (start, start + "s", start + "es")
    return word.startswith(start)


def category(kind: str, title: str, text: str, log: str = "") -> str:
    """The kind of error of a report: the category whose words appear most (the title counts three times), "crash"
    for a report of a closed program or a log that says so, "other" without any."""
    scores: Counter = Counter()
    for weight, source in ((3, title), (1, text)):
        for word in words(source):
            for key, _name, starts in CATEGORIES:
                if any(_matches(word, s) for s in starts):
                    scores[key] += weight * _GENERAL.get(key, 1.)
    if kind == "crash" or re.search(r"(?i)segmentation fault|access violation|terminate called|sigsegv|sigabrt", log or ""):
        scores["crash"] += 5
    if not scores:
        return "other"
    order = [key for key, _n, _s in CATEGORIES]
    return max(scores, key=lambda k: (scores[k], -order.index(k)))


class Corpus:
    """How many reports have each word, to weigh the words of one report (inverse document frequency)."""

    def __init__(self, texts: list[str] | None = None) -> None:
        self.count = 0
        self.df: Counter[str] = Counter()
        for text in texts or []:
            self.add(text)

    def add(self, text: str) -> None:
        self.count += 1
        self.df.update(set(terms(text)))

    def idf(self, term: str) -> float:
        return math.log((1 + self.count) / (1 + self.df[term])) + 1.


def keywords(text: str, corpus: Corpus, n: int = 8) -> list[str]:
    """The words that tell this report from the others: frequent in it, rare in the rest (TF-IDF)."""
    tf = Counter(terms(text))
    ranked = sorted(tf, key=lambda t: (-tf[t] * corpus.idf(t), t))
    return ranked[:n]


def printer_of(log: str) -> str:
    """The printer in use, from the lines Tisma attaches before the log ("Printer: name (model)")."""
    m = _PRINTER.search(log or "")
    if not m:
        return ""
    name = m.group(1).strip()
    model = re.search(r"\(([^()]+)\)\s*$", name)
    return model.group(1) if model and model.group(1) else name


def sentences(text: str) -> list[str]:
    out = []
    for s in _SENTENCE.split(text or ""):
        s = s.strip(" -*\t")
        if len(words(s)) >= 3:
            out.append(s)
    return out


def summary(category_key: str, title: str, texts: list[str], corpus: Corpus, error: str = "", printer: str = "",
            limit: int = 320) -> str:
    """A few lines for the team: the kind of error, the title, the sentences of the report that say most (with words
    rare among the reports; short ones preferred), the first error of the log and the printer."""
    candidates = []
    title_terms = set(terms(title))
    for position, s in enumerate(x for t in texts for x in sentences(t)):
        found = set(terms(s))
        if not found or found <= title_terms:
            continue
        score = sum(corpus.idf(t) for t in found - title_terms) / (1. + 0.05 * len(words(s)))
        candidates.append((score, position, s))
    chosen, used = [], 0
    top = max((c[0] for c in candidates), default=0.)
    for score, position, s in sorted(candidates, key=lambda c: -c[0]):
        # A second sentence only when it says nearly as much as the first.
        if chosen and (used + len(s) > limit or score < 0.6 * top):
            break
        chosen.append((position, s[:limit]))
        used += len(s)
        if len(chosen) == 2:
            break
    parts = [f"{CATEGORY_NAMES.get(category_key, category_key)}: {title.strip().rstrip('.')}."]
    parts += [s if s.endswith((".", "!", "?")) else s + "." for _p, s in sorted(chosen)]
    if error:
        parts.append(f"Error: {error[:200]}")
    if printer:
        parts.append(f"Impresora: {printer}.")
    return " ".join(parts)
