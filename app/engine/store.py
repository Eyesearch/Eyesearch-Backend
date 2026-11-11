"""The index store: every read and write of the page index.

Two rules shape this module.

**Persistence is one trip and it is atomic.** A page write and the matching
`crawl_queue` transition go into a single `BEGIN IMMEDIATE … COMMIT` batch. If
any statement in that batch fails, none of it lands and the URL is not marked
processed — which is the difference between "the crawl made progress" and "the
queue drained into nothing".

**Counts are proved, not assumed.** `persist_batch` reads `COUNT(*) FROM pages`
inside the same transaction that wrote the rows, so the number it reports is a
number the database confirmed, not a number the caller hoped for.
"""

from __future__ import annotations

import html
import json
import logging
import unicodedata
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.database import _dialect


def _host_limit_sql(column: str) -> str:
    """The frontier's per-host ceilings, as SQL. Imported lazily so the store
    does not depend on an agent at module load."""
    from app.agents import load_agent

    return load_agent("01_frontier_agent").host_limit_sql(column)
from app.schema_postgres import SEARCH_CONFIG
from app.engine.config import Settings, get_settings
from app.libsql_http import LibsqlClient, LibsqlError

from app.agents import load_agent

logger = logging.getLogger(__name__)

LanguageAgent = load_agent("08_language_agent").LanguageAgent
autosuggest = load_agent("39_autosuggest_trie_agent")

# Columns the FTS index carries, in bm25 weight order.
BM25_WEIGHTS = (10.0, 5.0, 1.0)

#: How much a title match is worth on top of bm25. bm25 is negative and
#: lower-is-better, so this is subtracted from the score to promote the row.
#: How much a page in the query's own language outranks one in another. Enough
#: to reorder otherwise-equal matches, not enough to lift a weak match over a
#: strong one.
#: How many index matches the fallback will look at. Bounded on purpose: this
#: path exists because the full ranking could not finish in time, and an
#: unbounded one would take just as long.
FAST_CANDIDATE_LIMIT = 500

LANGUAGE_MATCH_BOOST = 0.35

#: The architecture ceilings. Mirrored from the parser rather than imported, so
#: the store does not depend on an agent to know how wide its own columns are.
#: Kept in step with the parser's own cap. The store is the last line of
#: defence, not a second opinion: a title the parser allowed to 60 must not be
#: cut to 50 on the way in.
TITLE_CHAR_CAP = 60
BRAND_CHAR_CAP = 35
DESCRIPTION_CHAR_CAP = 300

#: Navigational intent. A query that *is* the brand or the domain name is not a
#: topic search — the reader wants that site — so it outranks ordinary relevance
#: by a wide margin, and the site's front door outranks its sub-pages.
NAVIGATIONAL_BOOST = 5.0
ROOT_PIN_BOOST = 10.0
#: A term found only in prose is a passing mention. On a one- or two-word query
#: that is how a blog outranks the site it is writing about.
DESCRIPTION_ONLY_PENALTY = 3.0
#: A gentle pull toward Indian domains, decided by the suffix.
REGIONAL_BOOST = 0.2
#: The description-only rule applies only to queries this short.
DESCRIPTION_ONLY_MAX_TOKENS = 2

TITLE_MATCH_BOOST = 1.0

#: Institutional Indian suffixes that earn a ranking boost.
AUTHORITY_SUFFIXES: tuple[str, ...] = (".gov.in", ".nic.in", ".ac.in", ".res.in")

#: How much an authoritative domain is worth. Larger than a title match: a
#: gazette notification on a `.gov.in` host is usually a better answer than a
#: blog post that happens to repeat the words.
AUTHORITY_DOMAIN_BOOST = 1.5

#: How many query terms are checked against the title.
MAX_TITLE_TOKENS = 5

#: How many lexical matches survive to be scored with the full boost expression.
#:
#: The boosts are six CASE expressions per row, several of them with regex and
#: string surgery over the domain. Measured on a 25k-page index, running them
#: over every match (25,164 rows for a common word) cost 1.6 s, almost all of it
#: in that expression. Ranking lexically first and boosting only the survivors
#: makes the cost the candidate window, not the corpus. The window is generous:
#: a page has to be outside the lexical top 500 to be lost, and the caller only
#: ever shows fifty.
#: How many lexically-best matches reach the boost pass. Measured on 73,943
#: pages with the crawler running: a fresh query took 5,791 ms at 500 and the
#: statement timeout is 6,000, so one common word was all it took to lose the
#: ranking entirely and answer from the index-order fallback. 300 keeps the
#: window well inside the budget; the rows it drops are the tail of a lexical
#: match, which the boosts were not going to rescue.
RANK_CANDIDATE_LIMIT = 300

#: How many matches the rank is computed over. `ORDER BY ts_rank(...) LIMIT n`
#: ranks every match before discarding most of them; on 73,943 pages that ran
#: past the 6 s statement timeout for a single common word. This bounds the
#: work to a slice that is still 40x the page size.
RANK_SCAN_LIMIT = 2000

#: How much a page's own quality moves it. `quality_score` runs 0.2 (a
#: JavaScript shell the crawler could barely read) to 1.0 (a page with prose and
#: a description), so this is worth 0.16 of score between the two ends - enough
#: to separate a shell from a real page of equal lexical match, not enough to
#: override a strong title hit.
QUALITY_BOOST = 0.2

#: Width of the passage returned as a match snippet, in characters.
SNIPPET_CHAR_CAP = 320


@dataclass
class PageDocument:
    """One page, ready to be stored."""

    url: str
    domain: str
    title: str
    description: str
    body_text: str
    quality_score: float = 1.0
    digest: str = ""
    category: str = "general_reference"
    language: str = "en"
    #: What the card shows: the publisher's own name, a breadcrumb, and a short
    #: action-oriented title instead of a keyword-stuffed <title>.
    brand_name: str = ""
    clean_url: str = ""
    action_title: str = ""
    #: Action links found on the page: [{"intent","label","url"}]. Empty for a
    #: page with none, which is the common case.
    sitelinks: list[dict[str, str]] = field(default_factory=list)
    #: Schema.org types from the page, used for classification only. Not a
    #: column: it describes the page, it is not part of the index.
    schema_types: list[str] = field(default_factory=list)
    category: str = ""
    category_code: str = ""
    sitelinks: list[dict[str, str]] = field(default_factory=list)
    #: Ranking signals, carried so a caller can tell a navigational hit from an
    #: ordinary one without re-deriving it.

    def as_row(self) -> tuple[Any, ...]:
        """The twelve bound values the page insert expects, in SQL order.

        `quality_score` is cast to `float` here rather than trusted from the
        caller: it is a REAL column, and an `int` reaching the encoder would be
        sent as an integer while a `Decimal` would be sent as text. Turso
        rejects both for a REAL column.
        """
        # The last line of defence. Every upstream agent applies these caps, and
        # this applies them again: a bug in any one of them should not be able to
        # put a 400-character paragraph where a 300-character description goes.
        return (
            self.domain,
            self.url,
            (self.title or "")[:TITLE_CHAR_CAP],
            (self.description or "")[:DESCRIPTION_CHAR_CAP],
            self.body_text,
            float(self.quality_score),
            self.digest,
            self.category,
            self.language,
            (self.brand_name or "")[:BRAND_CHAR_CAP],
            self.clean_url,
            (self.action_title or "")[:TITLE_CHAR_CAP],
            json.dumps(self.sitelinks or []),
        )


@dataclass
class QueuedUrl:
    """A row of `crawl_queue` handed to the pipeline."""

    rowid: int
    url: str
    domain: str
    retry_count: int = 0


@dataclass
class PersistOutcome:
    """What one persistence batch actually achieved."""

    pages_written: int = 0
    total_pages: int = 0
    sites_written: int = 0
    queue_closed: int = 0
    duplicates: int = 0
    committed: bool = False
    reason: str = ""
    #: The URLs this batch wrote. The crawl loop passes them to the search cache
    #: so cached answers mentioning them are dropped rather than served stale.
    urls: list[str] = field(default_factory=list)


@dataclass
class SearchHit:
    """One row of an index search."""

    page_id: int
    url: str
    title: str
    description: str
    body_text: str
    score: float = 0.0
    match_snippet: str = ""
    raw_score: float = 0.0
    brand_name: str = ""
    clean_url: str = ""
    action_title: str = ""
    #: Action links found on the page: [{"intent","label","url"}]. Empty for a
    #: page with none, which is the common case.
    sitelinks: list[dict[str, str]] = field(default_factory=list)
    #: Ranking signals, carried so a caller can tell a navigational hit from an
    #: ordinary one without re-deriving it.
    nav_hit: int = 0
    root_hit: int = 0
    region_hit: int = 0
    #: A sentence from the body containing the query, or "" when the query does
    #: not appear in the prose. Carries <b> around the match; the caller strips
    #: them because both shipped clients render plain text.
    dynamic_headline: str = ""
    #: True when this hit came from the index-only path taken after the
    #: ranked query timed out. The caller reports it: a quietly worse answer
    #: that looks like a good one is how a load problem becomes a mystery.
    degraded: bool = False
    #: Schema.org types from the page, used for classification only. Not a
    #: column: it describes the page, it is not part of the index.
    schema_types: list[str] = field(default_factory=list)
    category: str = ""
    category_code: str = ""
    sitelinks: list[dict[str, str]] = field(default_factory=list)
    #: Ranking signals, carried so a caller can tell a navigational hit from an
    #: ordinary one without re-deriving it.
    nav_hit: int = 0
    root_hit: int = 0
    region_hit: int = 0

    @property
    def snippet(self) -> str:
        """The passage that matched the query, else the description.

        `snippet()` from FTS5 picks the part of the page the query actually
        hit, which beats a fixed prefix of the body. When the match was on the
        title the snippet is the title again, so it falls back rather than
        repeating the heading back at the reader.
        """
        matched = " ".join((self.match_snippet or "").split())
        title = " ".join((self.title or "").split())
        if matched and matched.strip("…").strip() != title:
            return matched[:SNIPPET_CHAR_CAP]

        text = (self.description or "").strip()
        if text:
            return text

        body = " ".join((self.body_text or "").split())
        return body[:200] + ("…" if len(body) > 200 else "")


@dataclass
class Stats:
    sites: int = 0
    pages: int = 0
    indexed: int = 0
    queued: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sites": self.sites,
            "pages": self.pages,
            "indexed": self.indexed,
            "queued": self.queued,
            **self.extra,
        }


# ---------------------------------------------------------------------------
# FTS query building
# ---------------------------------------------------------------------------

_FTS_UNSAFE = set('"\':*()[]{}^~+-')


def fts_tokens(query: str) -> list[str]:
    """Split a user query into FTS-safe terms.

    FTS5 treats `-`, `:`, `*`, quotes and brackets as syntax, so a query of
    "covid-19: what now?" is a syntax error unless every term is quoted. Each
    token is therefore reduced to word characters and emitted as a quoted
    string.
    """
    tokens: list[str] = []
    for raw in (query or "").lower().split():
        cleaned = "".join(ch for ch in raw if _is_word_char(ch))
        cleaned = cleaned.strip("_/.")
        if cleaned:
            tokens.append(cleaned)
    return tokens


def _is_word_char(ch: str) -> bool:
    """Whether a character belongs in a search term.

    `str.isalnum()` is not enough: every Indian script writes its vowels as
    combining marks, and those are not alphanumeric. Keeping only alnum turned
    "ಕರ್ನಾಟಕ" into "ಕರನಟಕ" — the word with its vowels torn out — so a Kannada
    query matched nothing at all. PostgreSQL's own tokeniser handles these
    scripts correctly; the query builder was the thing mangling them.
    """
    if ch.isalnum():
        return True
    # Mn/Mc/Ms: non-spacing, spacing and enclosing combining marks.
    if unicodedata.category(ch).startswith("M"):
        return True
    return ch in "_/."


def build_match_query(query: str, *, prefix: bool = False) -> str:
    """Turn a user query into an FTS5 MATCH expression."""
    tokens = fts_tokens(query)
    if not tokens:
        return ""
    if prefix:
        return " OR ".join(f'"{token}"*' for token in tokens)
    return " AND ".join(f'"{token}"' for token in tokens)


def build_tsquery(query: str, *, prefix: bool = False) -> str:
    """Turn a user query into a PostgreSQL `to_tsquery` expression.

    Tokens are reduced to word characters first: tsquery treats `&`, `|`, `!`,
    `(`, `)`, `:` and `*` as syntax, and a stray one is a syntax error rather
    than a search for that character.
    """
    tokens = []
    for token in fts_tokens(query):
        # Same rule as the tokeniser: a combining mark is part of the word in
        # every Indian script, and dropping it turned "ಕರ್ನಾಟಕ" into
        # "ಕರನಟಕ" — a term that matches nothing.
        cleaned = "".join(ch for ch in token if _is_word_char(ch) and ch != ".")
        if cleaned:
            tokens.append(cleaned)
    if not tokens:
        return ""
    if prefix:
        return " | ".join(f"{token}:*" for token in tokens)
    return " & ".join(tokens)


def passage_around_match(text: str, tokens: Sequence[str], window: int = SNIPPET_CHAR_CAP) -> str:
    """The window of `text` around the first query term it contains.

    FTS5's own `snippet()` was the obvious tool and is what the brief asked
    for, but on the SQLite build this project runs on it ignores `colnum` and
    returns column 0 for every call — verified against both a plain and an
    external-content FTS5 table, where `highlight()` behaves correctly and
    `snippet()` does not. Rather than depend on behaviour that is wrong here
    and unverifiable on Turso, the window is computed in Python: portable,
    deterministic and testable.

    Returns `""` when no token appears, so the caller can fall back to the
    description instead of showing an arbitrary opening.
    """
    cleaned = " ".join((text or "").split())
    if not cleaned or not tokens:
        return ""

    lowered = cleaned.lower()
    positions = [lowered.find(token) for token in tokens if token]
    positions = [position for position in positions if position >= 0]
    if not positions:
        return ""

    match_at = min(positions)
    start = max(0, match_at - window // 3)
    end = min(len(cleaned), start + window)
    start = max(0, min(start, end - window))

    # Do not cut a word in half at either edge.
    if start > 0:
        space = cleaned.find(" ", start)
        if 0 <= space < match_at:
            start = space + 1
    if end < len(cleaned):
        space = cleaned.rfind(" ", match_at, end)
        if space > match_at:
            end = space

    passage = cleaned[start:end].strip()
    if start > 0:
        passage = "…" + passage
    if end < len(cleaned):
        passage = passage + "…"
    return passage


def highlight_terms(text: str, tokens: Sequence[str]) -> str:
    """Wrap query terms in `<b>…</b>`, escaping everything else.

    Both shipped clients render a snippet as plain text and do their own
    highlighting — the web UI with `<mark>` by splitting text nodes, the Android
    app with a Spanned string. So this is **off by default** and only applied
    when a caller asks for it (`?highlight=1`); sending markup unasked would
    show literal `<b>` tags in both apps.

    The text is untrusted page content, so every segment is HTML-escaped on the
    way out and only the tags added here are real markup. The scan runs over the
    original text and escapes as it builds, so a token can never match inside an
    escaped entity like `&amp;`.
    """
    if not text:
        return text

    unique = sorted({token for token in tokens if token}, key=len, reverse=True)
    if not unique:
        return html.escape(text, quote=False)

    pattern = re.compile(
        r"\b(" + "|".join(re.escape(token) for token in unique) + r")\b",
        re.IGNORECASE,
    )

    parts: list[str] = []
    cursor = 0
    for match in pattern.finditer(text):
        parts.append(html.escape(text[cursor : match.start()], quote=False))
        parts.append(f"<b>{html.escape(match.group(0), quote=False)}</b>")
        cursor = match.end()
    parts.append(html.escape(text[cursor:], quote=False))
    return "".join(parts)


def _sitelinks_from(value: Any) -> list[dict[str, str]]:
    """The stored sitelinks, or an empty list.

    psycopg hands JSONB back already parsed; a text column hands back a string.
    Both are accepted so the row mapper does not depend on the driver.
    """
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    return [
        {"intent": str(item.get("intent", "")), "label": str(item.get("label", "")),
         "url": str(item.get("url", ""))}
        for item in value
        if isinstance(item, dict) and item.get("url")
    ]


def _is_timeout(error: Exception) -> bool:
    """Whether a database error is a statement timeout rather than a real fault."""
    text = str(error).lower()
    return "statement timeout" in text or "canceling statement" in text or "cancelling statement" in text


def _query_language(query: str) -> str:
    """The language a query is written in, from its script.

    A Kannada query should find Kannada pages first. Detection is by Unicode
    block, so it works for every Indian script without a table of languages and
    without a word of domain knowledge — English, and any other Latin-script
    query, comes back as "en".
    """
    try:
        return LanguageAgent().detect(query or "")
    except Exception:  # noqa: BLE001 - a ranking boost is not worth an error
        return "en"


def normalise_score(raw: float) -> float:
    """bm25 is lower-is-better and unbounded; map it to 0..1 for the API."""
    try:
        value = abs(float(raw))
    except (TypeError, ValueError):
        return 0.0
    return round(1.0 / (1.0 + value), 4)


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


#: Cut a stored body at a word boundary, never mid-word. `substr(text, 1, n)`
#: cut "Karnataka is in southern India" to "Karnataka is in southern India, ne"
#: and the card showed the "ne" — dangling fragments read as corruption. This
#: mirrors `truncate_body` in the parser, and a test pins the two together.
SQL_TRIM_TO_WORD = (
    "CASE WHEN length(body_text) <= CAST(? AS INTEGER) THEN body_text "
    "WHEN length(regexp_replace(left(body_text, CAST(? AS INTEGER)), "
    "'\\s+\\S*$', '')) > 0 "
    "THEN rtrim(regexp_replace(left(body_text, CAST(? AS INTEGER)), "
    "'\\s+\\S*$', '')) "
    "ELSE left(body_text, CAST(? AS INTEGER)) END"
)

class IndexStore:
    """All index reads and writes, over one injected database client."""

    def __init__(self, client: LibsqlClient, settings: Settings | None = None) -> None:
        self._client = client
        self._settings = settings or get_settings()

    @property
    def client(self) -> LibsqlClient:
        return self._client

    async def ensure_schema(self) -> dict[str, int]:
        from app.database import initialize_database

        return await initialize_database(self._client)

    # -- writes ------------------------------------------------------------

    async def persist_batch(
        self,
        documents: Sequence[PageDocument],
        queue_ids: Sequence[int] = (),
    ) -> PersistOutcome:
        """Write pages and close their queue rows in one atomic batch."""
        if not documents and not queue_ids:
            return PersistOutcome(committed=True, reason="nothing to write")

        statements: list[tuple[str, Sequence[Any]]] = [("BEGIN IMMEDIATE", ())]

        for document in documents:
            statements.append(
                (
                    "INSERT INTO sites (domain, canonical_url) VALUES (?, ?) "
                    "ON CONFLICT(domain) DO NOTHING",
                    (document.domain, f"https://{document.domain}/"),
                )
            )

        for document in documents:
            statements.append(
                (
                    "INSERT INTO pages "
                    "(site_id, url, title, description, body_text, quality_score, digest, "
                    "category, language, brand_name, clean_url, action_title, sitelinks, "
                    "indexed_at) "
                    "VALUES ((SELECT site_id FROM sites WHERE domain = ?), ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?, ?, ?, CAST(? AS JSONB), CURRENT_TIMESTAMP) "
                    # Targeted at the URL. A bare DO NOTHING also swallowed the
                    # digest unique index, so a page whose opening 200 characters
                    # matched another page's — a shared template preamble — was
                    # dropped as a duplicate even though its URL had never been
                    # seen. That is the false-duplicate bug: accepted pages
                    # vanishing with "0 pages written".
                    "ON CONFLICT (url) DO NOTHING",
                    document.as_row(),
                )
            )

        for rowid in queue_ids:
            statements.append(
                (
                    "UPDATE crawl_queue SET status = 'processed', "
                    "processed_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (rowid,),
                )
            )

        # The strict per-page ceiling, applied in the same transaction as the
        # inserts so a row is never over it between two commits. It is flat: a
        # domain with one page gets the same 40 characters as one with 25.
        ceiling = int(self._settings.page_body_char_ceiling or 0)
        if ceiling > 0:
            for domain in {document.domain for document in documents if document.domain}:
                statements.append(
                    (
                        f"UPDATE pages SET body_text = {SQL_TRIM_TO_WORD} "
                        "WHERE site_id = (SELECT site_id FROM sites WHERE domain = ?) "
                        "AND length(body_text) > CAST(? AS INTEGER)",
                        (ceiling, ceiling, ceiling, ceiling, domain, ceiling),
                    )
                )

        # The domain total is the backstop: with 40 x 25 it cannot bite, but it
        # still holds if either the ceiling or the page cap is raised.
        budget = int(self._settings.domain_body_budget_chars or 0)
        if budget > 0:
            for domain in {document.domain for document in documents if document.domain}:
                statements.append(
                    (
                        "UPDATE pages SET body_text = substr(body_text, 1, "
                        "CAST(GREATEST(1, CAST(? AS INTEGER) / GREATEST(1, "
                        "(SELECT COUNT(*) FROM pages q WHERE q.site_id = pages.site_id))) "
                        "AS INTEGER)) "
                        "WHERE site_id = (SELECT site_id FROM sites WHERE domain = ?) "
                        "AND length(body_text) > GREATEST(1, CAST(? AS INTEGER) / "
                        "GREATEST(1, (SELECT COUNT(*) FROM pages q "
                        "WHERE q.site_id = pages.site_id)))",
                        (budget, domain, budget),
                    )
                )

        statements.append(("SELECT COUNT(*) FROM pages", ()))
        statements.append(("SELECT COUNT(*) FROM sites", ()))
        statements.append(("COMMIT", ()))

        try:
            results = await self._client.batch(statements)
        except LibsqlError as error:
            await self._rollback_quietly()
            logger.error("persistence batch failed: %s", error)
            return PersistOutcome(committed=False, reason=str(error))

        pages_start = 1 + len(documents)
        queue_start = pages_start + len(documents)
        page_inserts = results[pages_start:queue_start]
        queue_updates = results[queue_start : queue_start + len(queue_ids)]
        total_pages = int(results[-3].scalar(0) or 0)
        total_sites = int(results[-2].scalar(0) or 0)

        written = sum(1 for result in page_inserts if result.rows_affected > 0)
        closed = sum(1 for result in queue_updates if result.rows_affected > 0)

        # Now that the conflict target is the URL, "not written" means exactly
        # one thing: that URL is already in the table. The subtraction is honest
        # rather than a catch-all that also swallowed digest collisions.

        outcome = PersistOutcome(
            pages_written=written,
            total_pages=total_pages,
            sites_written=total_sites,
            queue_closed=closed,
            duplicates=max(0, len(documents) - written),
            committed=True,
            urls=[document.url for document in documents if document.url],
        )
        logger.info(
            "DB COMMIT SUCCESS: %d pages written, %d duplicates skipped, %d queue rows closed "
            "(index total: %d pages, %d sites)",
            outcome.pages_written,
            outcome.duplicates,
            outcome.queue_closed,
            outcome.total_pages,
            outcome.sites_written,
        )
        return outcome

    async def prune_queue(self, *, retain_days: int, limit: int) -> int:
        """Delete terminal queue rows older than the retention window.

        Bounded by `limit` so one call can never be a huge delete. `created_at`
        and `processed_at` are written by CURRENT_TIMESTAMP, so they compare
        directly against `now() + interval`, since callers pass a signed offset.

        Pruned URLs may be discovered and crawled again later; that is the
        intended trade for keeping the queue from growing without limit.
        """
        if retain_days <= 0 or limit <= 0:
            return 0
        result = await self._client.execute(
            "DELETE FROM crawl_queue WHERE id IN ("
            "  SELECT id FROM crawl_queue"
            "  WHERE status IN ('skipped', 'failed')"
            "    AND COALESCE(processed_at, created_at) < now() + ?::interval"
            "  LIMIT ?)",
            (f"-{int(retain_days)} days", int(limit)),
        )
        return int(result.rows_affected or 0)

    async def skip_pending_for_domain(self, domain: str) -> int:
        """Park every remaining pending URL for a domain that is at its cap.

        Filtering capped domains out of the dequeue is not enough on its own:
        their rows would sit at `pending` for ever, so `queue_depth()` keeps
        reporting a full queue and `enqueue()` drops genuinely new domains at
        `MAX_PENDING_QUEUE`. Marking them `skipped` keeps the depth honest.
        """
        if not domain:
            return 0
        result = await self._client.execute(
            "UPDATE crawl_queue SET status = 'skipped' "
            "WHERE status = 'pending' AND domain = ?",
            (domain,),
        )
        return result.rows_affected

    async def skip_pending_for_capped_domains(self, cap: int | None = None) -> int:
        """One-off reconciliation for rows queued before a host hit its ceiling.

        Each host is measured against its own tier's ceiling. `cap` is kept as an
        explicit override for a caller that wants one uniform number — the
        repair script does, to clear a backlog quickly.
        """
        having = (
            "COUNT(*) >= ?" if cap is not None else f"COUNT(*) >= ({_host_limit_sql('s.domain')})"
        )
        # The reason goes in `last_error`, which is the only free-text field the
        # queue has: a row parked here is otherwise indistinguishable from one
        # that failed a fetch, and `pending` reads as work that will never run.
        result = await self._client.execute(
            "UPDATE crawl_queue SET status = 'skipped', last_error = 'domain_cap',"
            " processed_at = CURRENT_TIMESTAMP"
            " WHERE status = 'pending' AND domain IN ("
            "  SELECT s.domain FROM sites s JOIN pages p ON p.site_id = s.site_id "
            f"  GROUP BY s.domain HAVING {having}"
            ")",
            (int(cap),) if cap is not None else (),
        )
        if result.rows_affected:
            logger.info(
                "marked %d queued URLs skipped: their host is at its page ceiling",
                result.rows_affected,
            )
        return result.rows_affected

    async def domain_page_counts(self, domains: Sequence[str]) -> dict[str, int]:
        """Indexed pages per domain, for the whole batch in one query.

        `pages` has no `domain` column — it is reached through `sites` — and the
        count is deliberately batched: one query per batch, never one per URL,
        because a per-URL count would put a database round trip in front of
        every fetch.
        """
        unique = [domain for domain in dict.fromkeys(domains) if domain]
        if not unique:
            return {}
        placeholders = ", ".join("?" for _ in unique)
        result = await self._client.execute(
            "SELECT s.domain, COUNT(*) FROM pages p "
            "JOIN sites s ON s.site_id = p.site_id "
            f"WHERE s.domain IN ({placeholders}) GROUP BY s.domain",
            tuple(unique),
        )
        return {str(row[0]): int(row[1]) for row in result.rows}

    async def mark_skipped(self, queue_ids: Sequence[int], reason: str = "") -> int:
        """Park queue rows that are deliberately not crawled.

        Distinct from `failed`: a skipped row is a policy decision (the domain
        is at its page cap), not an error, and re-arming never revives it.
        """
        if not queue_ids:
            return 0
        results = await self._client.batch(
            [
                ("UPDATE crawl_queue SET status = 'skipped' WHERE id = ?", (rowid,))
                for rowid in queue_ids
            ]
        )
        skipped = sum(1 for result in results if result.rows_affected > 0)
        if reason:
            logger.info("marked %d queue rows skipped: %s", skipped, reason)
        return skipped

    async def park_rows(
        self,
        failed_ids: Sequence[int] = (),
        skipped_ids: Sequence[int] = (),
    ) -> tuple[int, int]:
        """Close failed and skipped rows in a single round trip.

        These were two separate calls, and every round trip costs a slot
        against the rate limiter that bounds the whole crawl.
        """
        statements: list[tuple[str, Sequence[Any]]] = [
            (
                "UPDATE crawl_queue SET status = 'failed', retry_count = retry_count + 1 "
                "WHERE id = ?",
                (rowid,),
            )
            for rowid in failed_ids
        ]
        statements.extend(
            ("UPDATE crawl_queue SET status = 'skipped' WHERE id = ?", (rowid,))
            for rowid in skipped_ids
        )
        if not statements:
            return (0, 0)

        results = await self._client.batch(statements)
        failed = sum(1 for result in results[: len(failed_ids)] if result.rows_affected > 0)
        skipped = sum(1 for result in results[len(failed_ids) :] if result.rows_affected > 0)
        return failed, skipped

    async def mark_failed(self, queue_ids: Sequence[int], reason: str = "") -> int:
        """Park permanent/quality failures; they are never auto-retried."""
        if not queue_ids:
            return 0
        statements: list[tuple[str, Sequence[Any]]] = [
            (
                "UPDATE crawl_queue SET status = 'failed', retry_count = retry_count + 1, "
                "processed_at = CURRENT_TIMESTAMP, last_error = ? WHERE id = ?",
                (reason or "permanent_failure", rowid),
            )
            for rowid in queue_ids
        ]
        results = await self._client.batch(statements)
        failed = sum(1 for result in results if result.rows_affected > 0)
        if reason:
            logger.warning("marked %d queue rows failed: %s", failed, reason)
        return failed

    async def enqueue_urls(
        self,
        urls: Iterable[tuple[str, str]],
        chunk_size: int | None = None,
    ) -> int:
        """Queue `(url, domain)` pairs; already-known URLs are ignored.

        Sent in bounded chunks. A 120-page batch can discover hundreds of links,
        and one pipeline request carrying all of them overruns the HTTP response
        window — Turso answers too slowly and the client raises a ReadTimeout,
        losing the whole batch's discoveries. Chunking costs one extra request
        per fifty URLs against the rate limiter, which is far cheaper.

        Safe to split: these are ON CONFLICT DO NOTHING, so a retry or a partial run
        cannot duplicate a row.
        """
        pairs = list(urls)
        if not pairs:
            return 0

        size = chunk_size or self._settings.crawl_enqueue_chunk_size
        size = max(1, size)

        added = 0
        for start in range(0, len(pairs), size):
            chunk = pairs[start : start + size]
            results = await self._client.batch(
                [
                    (
                        # Gate one. ON CONFLICT only dedupes against the queue;
                        # a URL already in `pages` was queued again on every
                        # crawl that linked to it, and the worker then spent its
                        # batch on pages it had already indexed.
                        "INSERT INTO crawl_queue (url, domain, status) "
                        "SELECT ?, ?, 'pending' WHERE NOT EXISTS ("
                        "  SELECT 1 FROM pages p WHERE p.url = ?"
                        ") ON CONFLICT (url) DO NOTHING",
                        (url, domain, url),
                    )
                    for url, domain in chunk
                ]
            )
            added += sum(1 for result in results if result.rows_affected > 0)
        return added

    async def pending_domains(self, limit: int = 200) -> list[str]:
        """Which domains currently have work waiting."""
        result = await self._client.execute(
            "SELECT DISTINCT domain FROM crawl_queue WHERE status = 'pending' LIMIT ?",
            (limit,),
        )
        return [str(row[0]) for row in result.rows]

    async def rearm_processed(
        self,
        limit: int = 25,
        cooldown_seconds: int = 6 * 3600,
        domains: Sequence[str] | None = None,
    ) -> int:
        """Return the stalest crawled URLs to `pending` once their cooldown ends.

        Without this the frontier starves: once every seed is in `pages`, no
        new seed can be enqueued and the worker idles forever. Re-arming keeps
        the crawl alive and the index fresh, bounded by a cooldown so a URL is
        not re-fetched on every pass.
        """
        domain_clause = ""
        domain_args: list[Any] = []
        if domains:
            placeholders = ", ".join("?" for _ in domains)
            domain_clause = f" AND domain IN ({placeholders})"
            domain_args = list(domains)

        result = await self._client.execute(
            # Priority 0 marks this as a deliberate re-fetch. Without it the
            # row is indistinguishable from a stale duplicate and gate two would
            # skip it for ever.
            "UPDATE crawl_queue SET status = 'pending', priority = 0, "
            "processed_at = NULL "
            "WHERE id IN ("
            "  SELECT id FROM crawl_queue"
            "  WHERE status = 'processed'"
            "    AND (processed_at IS NULL OR processed_at <= now() + ?::interval)"
            "    AND domain NOT IN ("
            "      SELECT s.domain FROM sites s JOIN pages p ON p.site_id = s.site_id"
            "      GROUP BY s.domain HAVING COUNT(*) >= "
            f"      ({_host_limit_sql('s.domain')})"
            "    )"
            f"{domain_clause}"
            "  ORDER BY COALESCE(processed_at, '1970-01-01 00:00:00') ASC"
            "  LIMIT ?"
            ")",
            (
                f"-{int(cooldown_seconds)} seconds",
                *domain_args,
                int(limit),
            ),
        )
        return result.rows_affected

    async def rearm_retryable_failed(
        self,
        limit: int = 25,
        cooldown_seconds: int = 15 * 60,
        max_retries: int = 3,
    ) -> int:
        """Retry only transient fetch failures after a cooldown.

        Permanent HTTP 4xx, blocked hosts, parser failures and quality rejects
        never match this allowlist. A retry budget prevents dead hosts cycling
        through the frontier indefinitely.
        """
        result = await self._client.execute(
            "UPDATE crawl_queue SET status = 'pending', processed_at = NULL, last_error = NULL "
            "WHERE id IN ("
            " SELECT id FROM crawl_queue WHERE status = 'failed' AND retry_count <= ? "
            " AND processed_at IS NOT NULL AND processed_at <= now() + ?::interval "
            " AND (last_error = 'timeout' OR last_error LIKE 'http_error:%' "
            " OR last_error LIKE 'http_status:5%') "
            " AND domain NOT IN ("
            "   SELECT s.domain FROM sites s JOIN pages p ON p.site_id = s.site_id"
            "   GROUP BY s.domain HAVING COUNT(*) >= "
            f"   ({_host_limit_sql('s.domain')})"
            " )"
            " ORDER BY processed_at ASC LIMIT ?)",
            (
                int(max_retries),
                f"-{int(cooldown_seconds)} seconds",
                int(limit),
            ),
        )
        if result.rows_affected:
            logger.info("re-armed %d transiently failed URLs", result.rows_affected)
        return result.rows_affected

    async def mark_retryable_failed(self, queue_ids: Sequence[int], reason: str) -> int:
        """Record a transient fetch failure eligible for bounded re-arming."""
        if not queue_ids:
            return 0
        results = await self._client.batch([
            (
                "UPDATE crawl_queue SET status = 'failed', retry_count = retry_count + 1, "
                "processed_at = CURRENT_TIMESTAMP, last_error = ? WHERE id = ?",
                (reason, rowid),
            )
            for rowid in queue_ids
        ])
        return sum(1 for result in results if result.rows_affected > 0)

    async def update_page(self, document: PageDocument) -> bool:
        """Refresh an existing row in place — the revalidation path.

        `category` and `language` are deliberately left alone: they are set at
        ingestion and a revalidation pass would otherwise reset them to their
        defaults for every refreshed page.
        """
        result = await self._client.execute(
            "UPDATE pages SET title = ?, description = ?, body_text = ?, "
            "quality_score = ?, digest = ?, brand_name = ?, clean_url = ?, "
            "action_title = ?, indexed_at = CURRENT_TIMESTAMP WHERE url = ?",
            (
                document.title,
                document.description,
                document.body_text,
                document.quality_score,
                document.digest,
                document.brand_name,
                document.clean_url,
                document.action_title,
                document.url,
            ),
        )
        return result.rows_affected > 0

    async def _rollback_quietly(self) -> None:
        try:
            await self._client.execute("ROLLBACK")
        except LibsqlError:
            # No transaction was open; nothing to undo.
            pass

    # -- queue reads -------------------------------------------------------

    async def pending_batch(self, limit: int = 10) -> list[QueuedUrl]:
        """The next `limit` pending URLs, interleaved across domains.

        A plain `ORDER BY id` hands the whole batch to whichever domain was
        queued first: one portal with 40 pending URLs takes all ten slots and
        every other domain waits behind it. Numbering the rows per domain and
        ordering by that number gives round-robin — one URL from each domain,
        then a second from each — so a batch always spreads across the frontier.

        Domains that have already reached `MAX_PAGES_PER_DOMAIN` are excluded.
        Round-robin alone was not enough: once most domains are capped, every
        slot in the batch goes to a row the orchestrator will immediately skip,
        and a genuinely fresh domain queued behind them is never reached. The
        exclusion is one aggregate over `pages`; measured at 50k pages and 20k
        queued rows it is not slower than the window function it sits beside.

        The row number itself is not capped: capping it (say at two per domain)
        would shrink a batch below the configured size whenever few domains have
        work.
        """
        # The per-host ceiling comes from the frontier, generated from the same
        # constants the admission gate uses.
        result = await self._client.execute(
            "SELECT id, url, domain, retry_count FROM ("
            "  SELECT cq.id AS id, cq.url, cq.domain, cq.retry_count,"
            # Priority first, then age, inside each domain's turn: a re-queued
            # page is fetched before new discovery from the same site, without
            # letting one site take the whole batch.
            "         ROW_NUMBER() OVER (PARTITION BY cq.domain "
            "ORDER BY cq.priority ASC, cq.id ASC) AS rn "
            "  FROM crawl_queue cq WHERE cq.status = 'pending'"
            # Gate two. A row that is already indexed costs a fetch and writes
            # nothing. Priority 0 marks a deliberate re-fetch — a repair, or a
            # re-arm for freshness — and those must still run.
            "    AND (cq.priority = 0 OR NOT EXISTS ("
            "      SELECT 1 FROM pages p WHERE p.url = cq.url"
            "    ))"
            "    AND cq.domain NOT IN ("
            "      SELECT s.domain FROM sites s JOIN pages p ON p.site_id = s.site_id "
            "      GROUP BY s.domain HAVING COUNT(*) >= "
            f"      ({_host_limit_sql('s.domain')})"
            "    )"
            ") ORDER BY rn ASC, id ASC LIMIT ?",
            (limit,),
        )
        return [
            QueuedUrl(
                rowid=int(row[0]),
                url=str(row[1]),
                domain=str(row[2]),
                retry_count=int(row[3] or 0),
            )
            for row in result.rows
        ]

    async def queue_depth(self) -> int:
        result = await self._client.execute(
            "SELECT COUNT(*) FROM crawl_queue WHERE status = 'pending'"
        )
        return int(result.scalar(0) or 0)

    async def known_urls(self, urls: Sequence[str]) -> set[str]:
        """Which of these URLs are already indexed or already queued."""
        if not urls:
            return set()
        placeholders = ", ".join("?" for _ in urls)
        results = await self._client.batch(
            [
                (f"SELECT url FROM pages WHERE url IN ({placeholders})", tuple(urls)),
                (f"SELECT url FROM crawl_queue WHERE url IN ({placeholders})", tuple(urls)),
            ]
        )
        known = {str(row[0]) for row in results[0].rows}
        known.update(str(row[0]) for row in results[1].rows)
        return known

    async def indexed_urls(self, urls: Sequence[str]) -> set[str]:
        """Which of these URLs already have a page row.

        Admission asks this before a fetch: a URL whose canonical form is
        already in the index costs a fetch, a parse and one page of the host's
        quota to learn nothing. Only `pages` is consulted — every candidate is
        by definition in `crawl_queue`, so including the queue would match all
        of them.
        """
        if not urls:
            return set()
        placeholders = ", ".join("?" for _ in urls)
        result = await self._client.execute(
            f"SELECT url FROM pages WHERE url IN ({placeholders})", tuple(urls)
        )
        return {str(row[0]) for row in result.rows}

    # -- search ------------------------------------------------------------

    async def search(
        self,
        query: str,
        limit: int = 10,
        offset: int = 0,
        category: str | None = None,
        language: str | None = None,
    ) -> list[SearchHit]:
        """Weighted FTS5 search, best match first, optionally filtered."""
        match = build_match_query(query)
        if not match:
            return []


        filters = ""
        extra: list[Any] = []
        if category:
            # The filters sit inside the candidate window, so they narrow what
            # is ranked rather than only what is returned: a filtered search
            # must not spend its 500 candidates on rows it will then discard.
            filters += " AND p0.category = ?"
            extra.append(category)
        if language:
            filters += " AND p0.language = ?"
            extra.append(language)

        # Title boost. Bound parameters appear in SQL order, so the rank query
        # (in the SELECT) is bound before the match query (in the WHERE).
        tokens = fts_tokens(query)
        title_tokens = tokens[:MAX_TITLE_TOKENS]
        if title_tokens:
            title_terms = " OR ".join(
                "strpos(lower(coalesce(p.title, '')), ?) > 0" for _ in title_tokens
            )
        else:
            title_terms = "false"

        # Authority is read from `sites.domain`, the registrable domain the page
        # was actually indexed under — not from `p.url LIKE '%.gov.in/%'`, which
        # also matches a foreign page whose query string happens to contain that
        # text, and misses a bare `https://x.gov.in` with no trailing path.
        auth_terms = " OR ".join(
            f"s.domain LIKE '%{suffix}'" for suffix in AUTHORITY_SUFFIXES
        )

        # ts_rank is higher-is-better; bm25 was negative and lower-is-better.
        # Negating it here keeps the Python scoring, the boosts and the ORDER BY
        # exactly as they were, so none of the ranking maths had to change.
        rank_query = build_tsquery(query)
        match_query = build_tsquery(query, prefix=True) or rank_query
        if not rank_query:
            return []

        # Wrapped in a subquery: PostgreSQL exposes an output alias in ORDER BY
        # only as a bare reference, not inside an expression, and the ranking
        # here is `alias - boost * flag`.
        sql = (
            # ts_headline is the expensive part: it reads a body and finds a
            # fragment. Run over every candidate it costs O(matches) — a common
            # word matching 40,000 pages pays for 40,000 headlines to show ten.
            # Wrapping the ranked-and-sliced query means it runs on the rows
            # that survive LIMIT, so the cost is the page size, not the corpus.
            "SELECT top_slice.*, "
            "ts_headline('" + SEARCH_CONFIG + "', coalesce(top_slice.body_text, ''), "
            "to_tsquery('" + SEARCH_CONFIG + "', ?), "
            "'StartSel=<b>, StopSel=</b>, MaxWords=35, MinWords=15, ShortWord=3, "
            "HighlightAll=FALSE, MaxFragments=1') AS dynamic_headline "
            "FROM ("
            "SELECT * FROM ("
            # Nested twice: a select list cannot refer to its own aliases, so
            # description_only cannot test title_hit in the SELECT that defines
            # it. The inner query computes the hits, the middle derives the
            # flags, the outer orders.
            "SELECT base.*, CASE WHEN ? > 0 AND base.title_hit = 0 "
            "AND base.nav_hit = 0 THEN 1 ELSE 0 END AS description_only "
            "FROM ("
            "SELECT p.page_id, p.url, p.title, p.description, p.body_text, "
            "p.brand_name, p.clean_url, p.action_title, "
            f"(-ts_rank_cd(p.search_vector, to_tsquery('{SEARCH_CONFIG}', ?))) "
            "AS lexical_score, "
            f"CASE WHEN ({title_terms}) THEN 1 ELSE 0 END AS title_hit, "
            f"CASE WHEN ({auth_terms}) THEN 1 ELSE 0 END AS auth_hit, "
            # Language alignment: a Kannada query should surface Kannada pages.
            # The code is detected from the query's own script, so nothing is
            # hardcoded per language beyond the ISO mapping.
            "CASE WHEN p.language = ? THEN 1 ELSE 0 END AS lang_hit, "
            # Navigational: the query is the brand, or the domain's own name.
            #
            # `split_part(domain,'.',1)` returns the *first* label, which is the
            # subdomain when there is one: `www.phonepe.com` gave "www" and
            # `ir.phonepe.com` gave "ir", so neither matched a query of
            # "phonepe" on the name. Stripping `www.` first makes the label the
            # brand for an apex domain and leaves a real subdomain unmatched,
            # which is the distinction that matters.
            "CASE WHEN lower(split_part("
            "       regexp_replace(lower(coalesce(s.domain, '')), '^www\\.', ''), '.', 1)) "
            "     = lower(?) "
            "  OR (lower(coalesce(p.brand_name, '')) = lower(?) "
            "      AND ("
            "        array_length(string_to_array("
            "          regexp_replace(lower(coalesce(s.domain, '')), '^www\\.', ''), '.'), 1) = 2"
            "        OR lower(coalesce(s.domain, '')) ~ "
            "           '\\.(co|gov|ac|org|net|nic|res|edu|firm|gen|ind)\\.in$'"
            "      )) "
            "THEN 1 ELSE 0 END AS nav_hit, "
            # The site's front door, not one of its articles — and the *apex*
            # front door. A subdomain's own root was also matching here, so
            # `ir.phonepe.com` collected the root pin meant for the homepage.
            "CASE WHEN coalesce(s.domain, '') <> '' "
            "  AND ("
            "    array_length(string_to_array("
            "      regexp_replace(lower(s.domain), '^www\\.', ''), '.'), 1) = 2"
            "    OR lower(s.domain) ~ '\\.(co|gov|ac|org|net|nic|res|edu|firm|gen|ind)\\.in$'"
            "  ) "
            "  AND rtrim(p.url, '/') IN ("
            "  'https://' || s.domain, 'http://' || s.domain, "
            "  'https://www.' || s.domain, 'http://www.' || s.domain) "
            "THEN 1 ELSE 0 END AS root_hit, "
            # An Indian suffix, decided by the TLD rather than a name list.
            "CASE WHEN coalesce(s.domain, '') LIKE '%.in' THEN 1 ELSE 0 END AS region_hit, "
            # Appended last on purpose: the row mapper reads by position, and
            # inserting them earlier would shift every index after it.
            "p.category, c.code AS category_code, p.sitelinks, "
            # Carried into the outer sort so a page that only half exists - a
            # JavaScript shell, a two-line stub - cannot outrank a real one on
            # a lexical match alone. It was the one signal the ranking ignored.
            "COALESCE(p.quality_score, 1.0) AS quality "
            # Two-stage ranking. The inner select is the only one that reads
            # the whole match set, and it carries nothing but the lexically best
            # rows forward; the six boost expressions are then evaluated once
            # per candidate instead of once per match.
            # Only the id comes out of the window: selecting `p0.*` made the
            # scan detoast 25k bodies to hand 500 of them on, which cost more
            # than the boost expression it was meant to save. The row is
            # fetched again by primary key for the survivors.
            # The scan is bounded before the rank is computed, not after.
            # `ORDER BY ts_rank(...) LIMIT n` ranks *every* match and then
            # throws almost all of them away: on 73,943 pages a common word
            # matched enough rows that this alone ran past the 6 s statement
            # timeout, and the answer fell back to index order. Taking a
            # bounded slice of matches first makes the rank cost the slice,
            # not the corpus.
            f"FROM (SELECT s0.page_id, "
            # ts_rank rather than ts_rank_cd here: the cover-density variant is
            # the more expensive of the two and this stage only decides which
            # rows reach the boost pass, not their final order — the outer sort
            # still scores with ts_rank_cd. Measured on 25k matches: 456 ms
            # against 324 ms.
            f"ts_rank(s0.search_vector, to_tsquery('{SEARCH_CONFIG}', ?)) AS lex "
            f"FROM (SELECT p0.page_id, p0.search_vector FROM pages p0 "
            f"WHERE p0.search_vector @@ to_tsquery('{SEARCH_CONFIG}', ?){filters} "
            "LIMIT ?) s0 "
            "ORDER BY lex DESC LIMIT ?) cand "
            "JOIN pages p ON p.page_id = cand.page_id "
            "LEFT JOIN sites s ON s.site_id = p.site_id "
            "LEFT JOIN categories c ON c.id = p.category_id "
            ") base"
            ") ranked "
            "ORDER BY (lexical_score "
            "  - ? * title_hit - ? * auth_hit - ? * lang_hit "
            "  - ? * nav_hit - ? * root_hit * nav_hit "
            "  + ? * description_only "
            "  - ? * region_hit "
            "  - ? * quality) ASC "
            "LIMIT ? OFFSET ?"
            ") top_slice"
        )
        query_language = _query_language(query)

        navigational_term = " ".join((query or "").split())
        short_query = 1 if len(fts_tokens(query)) <= DESCRIPTION_ONLY_MAX_TOKENS else 0

        # Bound in the order the placeholders appear in the SQL text, which
        # begins with the outer select list: the short-query flag first, then
        # the inner SELECT, the inner WHERE, the weights and the window.
        # The outer select list is first in the text, so the headline's tsquery
        # is bound before anything inside the subquery.
        args = (
            match_query,
            short_query,
            rank_query,
            *title_tokens,
            query_language,
            navigational_term,
            navigational_term,
            rank_query,
            match_query,
            *extra,
            RANK_SCAN_LIMIT,
            RANK_CANDIDATE_LIMIT,
            TITLE_MATCH_BOOST,
            AUTHORITY_DOMAIN_BOOST,
            LANGUAGE_MATCH_BOOST,
            NAVIGATIONAL_BOOST,
            ROOT_PIN_BOOST,
            DESCRIPTION_ONLY_PENALTY,
            REGIONAL_BOOST,
            QUALITY_BOOST,
            limit,
            offset,
        )

        try:
            result = await self._client.execute(sql, args)
        except LibsqlError as error:
            # The ranked query ran past the statement timeout. Ranking every
            # match is the expensive part, so drop it: take a bounded set of
            # index matches and score those. A slower, cheaper answer beats the
            # "search timed out" the user was seeing.
            if not _is_timeout(error):
                raise
            logger.warning("ranked search timed out; falling back to index-only: %s", error)
            return await self._fast_search(match_query, filters, extra, limit, offset, tokens)

        hits = [self._row_to_hit(row, tokens) for row in result.rows]

        if hits:
            hits = await self._with_official_siblings(hits, limit)
            self._rescale_scores(hits)
            return hits

        # A strict AND is right for precision and wrong for a half-remembered
        # query. Fall back to OR rather than answering "nothing found".
        loose = build_tsquery(query, prefix=True)
        if loose and loose != match_query:
            # Same SQL, same bind order: the title terms, the MATCH term, the
            # filters, then both boosts. Leaving a boost out here is a bind-count
            # mismatch, and it only shows up on queries the strict AND misses.
            result = await self._client.execute(
                sql,
                (
                    match_query,
                    short_query,
                    rank_query,
                    *title_tokens,
                    query_language,
                    navigational_term,
                    navigational_term,
                    loose,
                    *extra,
                    RANK_SCAN_LIMIT,
                    TITLE_MATCH_BOOST,
                    AUTHORITY_DOMAIN_BOOST,
                    LANGUAGE_MATCH_BOOST,
                    NAVIGATIONAL_BOOST,
                    ROOT_PIN_BOOST,
                    DESCRIPTION_ONLY_PENALTY,
                    REGIONAL_BOOST,
                    QUALITY_BOOST,
                    limit,
                    offset,
                ),
            )
            hits = [self._row_to_hit(row, tokens) for row in result.rows]
            self._rescale_scores(hits)
        return hits

    async def _with_official_siblings(self, hits: list["SearchHit"], limit: int) -> list["SearchHit"]:
        """Put the named site's own pages directly behind its front door.

        A sub-page of the site the reader asked for rarely mentions the site's
        own name, so it does not match the query at all and the navigational
        result would sit alone with unrelated pages behind it.
        """
        if not hits or limit <= 1:
            return hits
        top = hits[0]
        if not getattr(top, "nav_hit", 0):
            return hits

        from urllib.parse import urlsplit

        try:
            domain = (urlsplit(top.url or "").hostname or "").lstrip("www.").lower()
        except ValueError:
            return hits
        if not domain:
            return hits

        seen = {hit.page_id for hit in hits}
        sql = (
            "SELECT p.page_id, p.url, p.title, p.description, p.body_text, "
            "p.brand_name, p.clean_url, p.action_title, 0.0 AS lexical_score, "
            "0 AS title_hit, 0 AS auth_hit, 0 AS lang_hit, 0 AS nav_hit, "
            "0 AS root_hit, 0 AS region_hit, p.category, c.code AS category_code, "
            "p.sitelinks, 0 AS description_only, '' AS dynamic_headline "
            "FROM pages p "
            "LEFT JOIN sites s ON s.site_id = p.site_id "
            "LEFT JOIN categories c ON c.id = p.category_id "
            "WHERE s.domain = ? "
            "ORDER BY p.quality_score DESC, p.page_id ASC LIMIT ?"
        )
        result = await self._client.execute(sql, (domain, limit))
        siblings = [
            self._row_to_hit(row, []) for row in result.rows if int(row[0]) not in seen
        ]
        if not siblings:
            return hits
        return ([top, *siblings, *hits[1:]])[:limit]

    async def _fast_search(
        self,
        match_query: str,
        filters: str,
        extra: list[Any],
        limit: int,
        offset: int,
        tokens: list[str],
    ) -> list["SearchHit"]:
        """Index-only retrieval: no ts_rank_cd, no full-table scoring.

        This is the path taken when the ranked query runs past the statement
        timeout. Ranking every match is the expensive part, so it is dropped:
        the GIN index narrows to a bounded candidate set and only those are
        read. What is given up is ranking across every match — a query matching
        most of the index returns the first candidates in page order rather than
        the best ones, which is the right trade against a timeout.
        """
        sql = (
            "SELECT p.page_id, p.url, p.title, p.description, p.body_text, "
            "p.brand_name, p.clean_url, p.action_title, "
            "0.0 AS lexical_score, 0 AS title_hit, 0 AS auth_hit, 0 AS lang_hit, "
            "0 AS nav_hit, 0 AS root_hit, 0 AS region_hit, "
            "p.category, c.code AS category_code, p.sitelinks, 0 AS description_only "
            "FROM pages p "
            "LEFT JOIN sites s ON s.site_id = p.site_id "
            "LEFT JOIN categories c ON c.id = p.category_id "
            f"WHERE p.search_vector @@ to_tsquery('{SEARCH_CONFIG}', ?){filters} "
            "ORDER BY p.page_id "
            "LIMIT ? OFFSET ?"
        )
        args: list[Any] = [match_query, *extra, FAST_CANDIDATE_LIMIT, offset]
        result = await self._client.execute(sql, args)
        hits = [self._row_to_hit(row, tokens) for row in result.rows]
        for hit in hits:
            hit.degraded = True
        self._rescale_scores(hits)
        return hits

    @staticmethod
    def _row_to_hit(row: Sequence[Any], tokens: Sequence[str] = ()) -> SearchHit:
        # Column order, from the SELECT in `search`: 0 page_id, 1 url, 2 title,
        # 3 description, 4 body_text, 5 brand_name, 6 clean_url, 7 action_title,
        # 8 lexical_score, 9 title_hit, 10 auth_hit, 11 lang_hit, 12 category,
        # 13 category_code, 14 sitelinks. The language boost was inserted before
        # the category columns and every index after it moved by one — the
        # category was reading the boost flag and the code was reading the
        # category name, which happened to render correctly and hid it.
        title_hit = int(row[9] or 0)
        auth_hit = int(row[10] or 0)
        boosted = (
            float(row[8] or 0.0)
            - TITLE_MATCH_BOOST * title_hit
            - AUTHORITY_DOMAIN_BOOST * auth_hit
        )
        body = str(row[4] or "")
        return SearchHit(
            page_id=int(row[0]),
            url=str(row[1] or ""),
            title=str(row[2] or ""),
            description=str(row[3] or ""),
            body_text=body,
            score=normalise_score(boosted),
            match_snippet=passage_around_match(body, tokens),
            raw_score=boosted,
            brand_name=str(row[5] or ""),
            clean_url=str(row[6] or ""),
            action_title=str(row[7] or ""),
            # Read by position. A wrong index does not raise — it silently
            # returns another column's value, which is how the category once
            # came back as a ranking flag.
            #   8 lexical, 9 title_hit, 10 auth_hit, 11 lang_hit, 12 nav_hit,
            #   13 root_hit, 14 region_hit, 15 category, 16 category_code,
            #   17 sitelinks, 18 description_only.
            category=str(row[15] or ""),
            category_code=str(row[16] or ""),
            sitelinks=_sitelinks_from(row[17] if len(row) > 17 else None),
            nav_hit=int(row[12] or 0) if len(row) > 12 else 0,
            root_hit=int(row[13] or 0) if len(row) > 13 else 0,
            region_hit=int(row[14] or 0) if len(row) > 14 else 0,
            # The headline is the last column of the ranked query (`SELECT
            # top_slice.*, ts_headline(...)`) but the fast path has no headline
            # at all, so it is read from the end rather than by a fixed index:
            # adding a column to `top_slice` silently shifted this to the wrong
            # value, and the snippet went empty instead of wrong, which is worse.
            dynamic_headline=(
                str(row[-1] or "") if len(row) > 19 else ""
            ),
        )

    @staticmethod
    def _rescale_scores(hits: Sequence[SearchHit]) -> None:
        """Spread `score` across the page of results, best first.

        bm25 is negative and lower-is-better, and the title boost pushes a
        good row below zero — so mapping it absolutely would make a boosted
        result look *worse* than an unboosted one. Rescaling against the page
        keeps the number monotone with the order the caller actually receives.
        It is relative within one page of results, not comparable across pages.
        """
        if not hits:
            return
        best = min(hit.raw_score for hit in hits)
        worst = max(hit.raw_score for hit in hits)
        span = worst - best
        for hit in hits:
            hit.score = 1.0 if span <= 0 else round((worst - hit.raw_score) / span, 4)

    async def suggest(self, query: str, limit: int = 6) -> list[str]:
        """Prefix completions: the words the index actually contains.

        This used to return whole page titles, which is a poor suggestion: a
        title is a sentence, and a dropdown of sentences is not a completion. The
        vocabulary is read from the search vectors themselves, so a suggestion is
        always a term that will match.
        """
        # The rule — what a suggestion is, and how short a prefix may be — is
        # the trie agent's, so the two suggestion paths cannot disagree.
        if not autosuggest.is_suggestable(query):
            return []

        try:
            result = await self._client.execute(
                autosuggest.suggestion_query(query),
                autosuggest.suggestion_params(query, limit),
            )
            words = [str(row[0]) for row in result.rows if row[0]]
            if words:
                return words
        except Exception as error:  # noqa: BLE001 - fall through to titles
            logger.debug("ts_stat suggest failed for %r: %s", query, error)

        match = build_tsquery(query, prefix=True)
        if not match:
            return []
        result = await self._client.execute(
            "SELECT p.title FROM pages p "
            f"WHERE p.search_vector @@ to_tsquery('{SEARCH_CONFIG}', ?) "
            "AND p.title <> '' "
            f"ORDER BY ts_rank_cd(p.search_vector, to_tsquery('{SEARCH_CONFIG}', ?)) DESC "
            "LIMIT ?",
            (match, match, limit),
        )
        seen: set[str] = set()
        titles: list[str] = []
        for row in result.rows:
            title = " ".join(str(row[0] or "").split())
            if title and title.lower() not in seen:
                seen.add(title.lower())
                titles.append(title)
        return titles

    async def stats(self) -> Stats:
        results = await self._client.batch(
            [
                ("SELECT COUNT(*) FROM sites", ()),
                ("SELECT COUNT(*) FROM pages", ()),
                # Counting the rows that are *missing* a vector, not the rows
                # that have one: the partial index `idx_pages_vector_missing`
                # answers this instantly, while the inverse comparison reads a
                # tsvector on every page and times out on a large index.
                (
                    "SELECT COUNT(*) FROM pages WHERE search_vector IS NULL"
                    if _dialect(self._client) == "postgres"
                    else "SELECT COUNT(*) FROM page_fts",
                    (),
                ),
                ("SELECT COUNT(*) FROM crawl_queue WHERE status = 'pending'", ()),
            ]
        )
        pages = int(results[1].scalar(0) or 0)
        if _dialect(self._client) == "postgres":
            indexed = max(0, pages - int(results[2].scalar(0) or 0))
        else:
            indexed = int(results[2].scalar(0) or 0)
        return Stats(
            sites=int(results[0].scalar(0) or 0),
            pages=pages,
            indexed=indexed,
            queued=int(results[3].scalar(0) or 0),
        )

    # -- revalidation ------------------------------------------------------

    async def stale_pages(self, limit: int = 5, older_than_seconds: int = 7 * 24 * 3600) -> list[str]:
        result = await self._client.execute(
            "SELECT url FROM pages WHERE indexed_at <= now() + ?::interval "
            "ORDER BY indexed_at ASC LIMIT ?",
            (f"-{int(older_than_seconds)} seconds", limit),
        )
        return [str(row[0]) for row in result.rows]

    async def get_page(self, url: str) -> dict[str, Any] | None:
        result = await self._client.execute(
            "SELECT page_id, url, title, description, body_text, category, language "
            "FROM pages WHERE url = ?",
            (url,),
        )
        row = result.first()
        if row is None:
            return None
        return {
            "page_id": int(row[0]),
            "url": str(row[1]),
            "title": str(row[2] or ""),
            "description": str(row[3] or ""),
            "body_text": str(row[4] or ""),
            "category": str(row[5] or "general_reference"),
            "language": str(row[6] or "en"),
        }


__all__ = [
    "BM25_WEIGHTS",
    "MAX_TITLE_TOKENS",
    "SNIPPET_CHAR_CAP",
    "TITLE_MATCH_BOOST",
    "highlight_terms",
    "passage_around_match",
    "IndexStore",
    "PageDocument",
    "PersistOutcome",
    "QueuedUrl",
    "SearchHit",
    "Stats",
    "build_match_query",
    "fts_tokens",
    "normalise_score",
]
