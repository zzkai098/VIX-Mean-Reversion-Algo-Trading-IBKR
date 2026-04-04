"""
VIX News Sentiment Engine — GDELT + VADER

Fetches US-Iran geopolitical headlines from GDELT GKG every NEWS_CACHE_MINUTES,
scores them with VADER, and returns a compound sentiment score in [-1, +1].

  +1.0 = maximally positive (de-escalation, peace talks)
   0.0 = neutral / unavailable
  -1.0 = maximally negative (war escalation, attacks)

Designed to be instantiated once in VIXStrategyEngine and passed into
generate_signal() on each call. Thread-safe for single-threaded use.
"""
import json
import urllib.parse
import urllib.request
from datetime import datetime

import nltk
from nltk.sentiment.vader import SentimentIntensityAnalyzer

from vix_config import (
    NEWS_CACHE_MINUTES,
    NEWS_KEYWORDS,
    VERBOSE,
)

GDELT_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"


class VIXNewsEngine:
    """
    Fetches and scores US-Iran geopolitical news from GDELT GKG.
    Caches results for NEWS_CACHE_MINUTES to avoid redundant HTTP requests.
    Returns neutral score (0.0) gracefully on any fetch/parse error.
    """

    def __init__(self, verbose: bool = VERBOSE):
        self._cache_score: float = 0.0
        self._cache_confidence: float = 0.0
        self._cache_article_count: int = 0
        self._cache_timestamp: datetime | None = None
        self._vader = SentimentIntensityAnalyzer()
        self.verbose = verbose

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_sentiment(self) -> dict:
        """
        Returns:
            {
                'score':         float,       # VADER compound, -1 to +1
                'confidence':    float,       # 0.0–1.0, based on article count
                'article_count': int,
                'cached':        bool,
                'error':         str | None,
            }
        Never raises. Returns score=0.0 on any failure.
        """
        if self._cache_is_fresh():
            return self._make_result(cached=True, error=None)

        try:
            self._refresh_cache()
            return self._make_result(cached=False, error=None)
        except Exception as e:
            stale = self._get_stale_fallback()
            if stale is not None:
                if self.verbose:
                    print(f"[NEWS] Fetch failed ({e}). Using stale cache (confidence×0.3).")
                return stale
            if self.verbose:
                print(f"[NEWS] Fetch failed ({e}). Using neutral sentiment.")
            return {"score": 0.0, "confidence": 0.0, "article_count": 0,
                    "cached": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _make_result(self, cached: bool, error: str | None) -> dict:
        return {
            "score": self._cache_score,
            "confidence": self._cache_confidence,
            "article_count": self._cache_article_count,
            "cached": cached,
            "error": error,
        }

    def _cache_is_fresh(self) -> bool:
        if self._cache_timestamp is None:
            return False
        age_min = (datetime.now() - self._cache_timestamp).total_seconds() / 60
        return age_min < NEWS_CACHE_MINUTES

    def _get_stale_fallback(self) -> dict | None:
        """Return stale cache at reduced confidence if < 60 min old, else None."""
        if self._cache_timestamp is None:
            return None
        age_min = (datetime.now() - self._cache_timestamp).total_seconds() / 60
        if age_min < 60:
            return {
                "score": self._cache_score,
                "confidence": self._cache_confidence * 0.3,
                "article_count": self._cache_article_count,
                "cached": True,
                "error": "stale_fallback",
            }
        return None

    # ------------------------------------------------------------------
    # Fetch + score
    # ------------------------------------------------------------------

    def _build_url(self) -> str:
        keyword_query = " OR ".join(f'"{kw}"' for kw in NEWS_KEYWORDS)
        params = {
            "query":      keyword_query,
            "mode":       "artlist",
            "maxrecords": "75",
            "format":     "json",
            "timespan":   "30min",
            "sourcelang": "eng",
        }
        return GDELT_BASE + "?" + urllib.parse.urlencode(params)

    def _refresh_cache(self) -> None:
        """Fetch GDELT articles, score with VADER, update cache. Raises on error."""
        url = self._build_url()
        req = urllib.request.Request(url, headers={"User-Agent": "VIXNewsEngine/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)

        texts = self._parse_gdelt_response(data)
        score, confidence = self._score_articles(texts)

        self._cache_score = score
        self._cache_confidence = confidence
        self._cache_article_count = len(texts)
        self._cache_timestamp = datetime.now()

        if self.verbose:
            print(f"[NEWS] Refreshed: {len(texts)} relevant articles | "
                  f"score={score:+.3f} conf={confidence:.2f}")

    def _parse_gdelt_response(self, data: dict) -> list[str]:
        """Extract keyword-relevant article titles from GDELT artlist response."""
        articles = data.get("articles", [])
        texts = []
        for art in articles:
            title = art.get("title", "").strip()
            if not title:
                continue
            title_lower = title.lower()
            if any(kw.lower() in title_lower for kw in NEWS_KEYWORDS):
                texts.append(title)
        return texts

    def _score_articles(self, texts: list[str]) -> tuple[float, float]:
        """
        Run VADER on each title.
        Returns (mean_compound_score, confidence).
        confidence = min(1.0, article_count / 10)
        """
        if not texts:
            return 0.0, 0.0
        scores = [self._vader.polarity_scores(t)["compound"] for t in texts]
        mean_score = sum(scores) / len(scores)
        confidence = min(1.0, len(scores) / 10.0)
        return mean_score, confidence
