"""Kleine BM25-index in het geheugen. Geen externe diensten, deterministisch."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter

TOKEN = re.compile(r"\w+", re.UNICODE)
STOPWORDS = frozenset(
    "de het een en of van in op te voor met aan is zijn wat hoe je jij u uw ik we wij die dat dit er"
    " the a an and or of in on to for with at is are be what how you your i we it this that"
    " as by from".split()
)


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


SUFFIXES = ("ingen", "ings", "ing", "en", "es", "s", "e")


def stem(token: str) -> str:
    """Grove suffix-stripper voor NL en EN, zodat price/pricing en module/modules samenvallen."""
    for suffix in SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    return token


def tokens(text: str) -> list[str]:
    return [stem(t) for t in TOKEN.findall(fold(text)) if t not in STOPWORDS and len(t) > 1]


class Bm25:
    def __init__(self, docs: list[tuple[str, str]], k1: float = 1.4, b: float = 0.75):
        """docs: (id, tekst). Titel en samenvatting worden door de aanroeper herhaald voor gewicht."""
        self.ids = [doc_id for doc_id, _ in docs]
        self.tfs = [Counter(tokens(text)) for _, text in docs]
        self.lengths = [sum(tf.values()) for tf in self.tfs]
        self.avg = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        df: Counter = Counter()
        for tf in self.tfs:
            df.update(tf.keys())
        n = len(docs)
        self.idf = {term: math.log(1 + (n - freq + 0.5) / (freq + 0.5)) for term, freq in df.items()}
        self.k1 = k1
        self.b = b

    def search(self, query: str, limit: int) -> list[tuple[str, float]]:
        terms = tokens(query)
        if not terms or not self.ids:
            return []
        scores = []
        for index, tf in enumerate(self.tfs):
            score = 0.0
            norm = self.k1 * (1 - self.b + self.b * self.lengths[index] / (self.avg or 1.0))
            for term in terms:
                freq = tf.get(term)
                if freq:
                    score += self.idf[term] * freq * (self.k1 + 1) / (freq + norm)
            if score > 0:
                scores.append((self.ids[index], score))
        scores.sort(key=lambda item: (-item[1], item[0]))
        return scores[:limit]
