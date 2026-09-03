"""Self-contained HTML dashboard.

No external assets: charts are inline SVG generated here rather than pulled
from a CDN, so the file opens from disk, survives being emailed, and works
behind a client firewall.

Colour choices: sentiment uses a blue/orange divergent pair rather than
red/green, which is indistinguishable for the ~8% of men with red-green colour
vision deficiency. Sentiment is also encoded by position and label, never by
colour alone.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime
from pathlib import Path

from igpulse.analyze.metrics import FigureMetrics, NarrativeMetrics, SentimentMix
from igpulse.analyze.sentiment import SentimentSummary
from igpulse.analyze.themes import ThemeTerm
from igpulse.config import AppConfig

logger = logging.getLogger(__name__)

_POSITIVE = "#2563eb"   # blue
_NEGATIVE = "#ea580c"   # orange
_NEUTRAL = "#94a3b8"    # slate
_UNCERTAIN = "#cbd5e1"  # light slate

_CSS = """
:root {
  --bg: #f8fafc; --panel: #ffffff; --ink: #0f172a; --muted: #64748b;
  --line: #e2e8f0; --accent: #1f3a5f;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0f172a; --panel: #1e293b; --ink: #f1f5f9; --muted: #94a3b8;
    --line: #334155; --accent: #93c5fd;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
.wrap { max-width: 1100px; margin: 0 auto; padding: 32px 20px 64px; }
h1 { font-size: 26px; margin: 0 0 4px; color: var(--accent); }
h2 { font-size: 18px; margin: 36px 0 12px; border-bottom: 1px solid var(--line);
  padding-bottom: 6px; }
.sub { color: var(--muted); margin: 0 0 8px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px,1fr));
  gap: 12px; margin: 20px 0; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
  padding: 14px 16px; }
.card .n { font-size: 24px; font-weight: 600; }
.card .l { color: var(--muted); font-size: 12px; text-transform: uppercase;
  letter-spacing: .04em; }
.panel { background: var(--panel); border: 1px solid var(--line);
  border-radius: 8px; padding: 16px; overflow-x: auto; }
table { border-collapse: collapse; width: 100%; min-width: 640px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line);
  white-space: nowrap; }
th { font-size: 12px; text-transform: uppercase; letter-spacing: .04em;
  color: var(--muted); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.tag { display: inline-block; padding: 1px 7px; border-radius: 99px;
  font-size: 11px; background: var(--line); color: var(--muted); }
.legend { display: flex; gap: 16px; flex-wrap: wrap; margin: 8px 0 4px;
  font-size: 12px; color: var(--muted); }
.legend i { width: 10px; height: 10px; border-radius: 2px; display: inline-block;
  margin-right: 5px; vertical-align: middle; }
.note { color: var(--muted); font-size: 12px; margin-top: 10px; }
.terms { color: var(--muted); font-size: 13px; }
.muted { color: var(--muted); }
h3 { font-size: 14px; margin: 18px 0 8px; color: var(--muted);
  text-transform: uppercase; letter-spacing: .04em; }
.sample { border-left: 3px solid var(--line); padding: 8px 0 8px 14px;
  margin-bottom: 14px; }
.sample-meta { font-size: 12px; color: var(--muted); margin-bottom: 4px;
  font-variant-numeric: tabular-nums; }
.sample-meta a { color: var(--accent); text-decoration: none; }
.sample-meta a:hover { text-decoration: underline; }
.sample-body { font-size: 13.5px; white-space: pre-wrap; word-break: break-word; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: .9em; background: var(--line); padding: 1px 4px; border-radius: 3px; }
"""


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _bar_chart(rows: list[tuple[str, int]], *, width: int = 640) -> str:
    """Horizontal bar chart as inline SVG, sorted descending."""
    if not rows:
        return '<p class="note">No data.</p>'
    label_w, row_h, gap = 190, 22, 6
    peak = max(v for _, v in rows) or 1
    height = len(rows) * (row_h + gap)
    bar_w = width - label_w - 70

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="Post volume by narrative">'
    ]
    for i, (label, value) in enumerate(rows):
        y = i * (row_h + gap)
        w = max(2, int(bar_w * value / peak))
        clipped = label if len(label) <= 26 else label[:25] + "…"
        parts.append(
            f'<text x="0" y="{y + 15}" font-size="12" fill="currentColor">'
            f'{_esc(clipped)}</text>'
            f'<rect x="{label_w}" y="{y + 3}" width="{w}" height="{row_h - 6}" '
            f'rx="3" fill="{_POSITIVE}" opacity="0.85"><title>'
            f'{_esc(label)}: {value:,}</title></rect>'
            f'<text x="{label_w + w + 8}" y="{y + 15}" font-size="12" '
            f'fill="currentColor" opacity="0.7">{value:,}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _sentiment_bar(mix: SentimentMix, *, width: int = 240) -> str:
    """Stacked proportion bar. Always accompanied by a numeric label."""
    if mix.total == 0:
        return '<span class="tag">no data</span>'
    segments = [
        (mix.positive, _POSITIVE, "positive"),
        (mix.neutral, _NEUTRAL, "neutral"),
        (mix.negative, _NEGATIVE, "negative"),
        (mix.uncertain, _UNCERTAIN, "uncertain"),
    ]
    parts = [
        f'<svg viewBox="0 0 {width} 14" width="{width}" height="14" role="img" '
        f'aria-label="Sentiment split">'
    ]
    x = 0.0
    for count, colour, name in segments:
        if not count:
            continue
        w = width * count / mix.total
        parts.append(
            f'<rect x="{x:.1f}" y="0" width="{w:.1f}" height="14" fill="{colour}">'
            f'<title>{name}: {count:,}</title></rect>'
        )
        x += w
    parts.append("</svg>")
    return "".join(parts)


def _fmt_net(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.2f}"


def _fmt_delta(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.1f}%"


def build_dashboard(
    cfg: AppConfig,
    *,
    narratives: list[NarrativeMetrics],
    figures: list[FigureMetrics],
    themes: list[ThemeTerm],
    sentiment: SentimentSummary,
    generated_at: datetime,
    samples: dict[str, list[dict]] | None = None,
    provenance: dict | None = None,
    window: dict | None = None,
    output_path: Path | None = None,
) -> Path:
    out_dir = Path(cfg.settings.report.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    local_time = generated_at.astimezone(cfg.timezone)
    if output_path is None:
        output_path = out_dir / (
            f"{cfg.settings.project.client_label}_"
            f"{local_time:%Y%m%d_%H%M}_dashboard.html"
        )

    sections = cfg.settings.report.html.include_sections
    total_posts = sum(n.post_count for n in narratives)
    total_comments = sum(n.comment_count for n in narratives)
    total_authors = sum(n.distinct_authors for n in narratives)

    body: list[str] = [
        '<div class="wrap">',
        "<h1>Instagram Discourse Dashboard</h1>",
        f'<p class="sub">{_esc(cfg.settings.project.client_label)} &middot; '
        f'{local_time:%d %B %Y, %H:%M} '
        f'({_esc(cfg.settings.project.timezone)})</p>',
        '<div class="cards">',
        f'<div class="card"><div class="n">{total_posts:,}</div>'
        f'<div class="l">Posts</div></div>',
        f'<div class="card"><div class="n">{total_comments:,}</div>'
        f'<div class="l">Comments</div></div>',
        f'<div class="card"><div class="n">{total_authors:,}</div>'
        f'<div class="l">Distinct authors</div></div>',
        f'<div class="card"><div class="n">{len(narratives)}</div>'
        f'<div class="l">Narratives</div></div>',
        f'<div class="card"><div class="n">{sentiment.coverage * 100:.0f}%</div>'
        f'<div class="l">Sentiment coverage</div></div>',
        "</div>",
        '<div class="legend">'
        f'<span><i style="background:{_POSITIVE}"></i>Positive</span>'
        f'<span><i style="background:{_NEUTRAL}"></i>Neutral</span>'
        f'<span><i style="background:{_NEGATIVE}"></i>Negative</span>'
        f'<span><i style="background:{_UNCERTAIN}"></i>Uncertain</span>'
        "</div>",
    ]

    # What was actually searched. Without this the reader cannot tell whether a
    # low count means low discourse or a badly-chosen tag.
    if "narrative" in sections and cfg.narratives.narratives:
        body.append("<h2>What was searched</h2><div class='panel'><table>")
        body.append(
            "<tr><th>Narrative</th><th>Hashtags collected</th>"
            "<th>Caption filters</th><th class='num'>Lookback</th></tr>"
        )
        ncfg = cfg.settings.ingest.narrative
        for n in cfg.narratives.narratives:
            tags = " ".join(f"<span class='tag'>#{_esc(t)}</span>"
                            for t in n.hashtags)
            filters = (
                ", ".join(_esc(t) for t in n.terms)
                if n.terms else "<span class='muted'>none — all posts kept</span>"
            )
            body.append(
                f"<tr><td>{_esc(n.label)}</td><td>{tags}</td>"
                f"<td class='terms'>{filters}</td>"
                f"<td class='num'>{_esc(ncfg.lookback)}</td></tr>"
            )
        body.append("</table>")
        if window:
            body.append(
                f"<p class='note'>Posts actually collected span "
                f"{window['oldest']:%d %b %Y} to {window['newest']:%d %b %Y}. "
                f"A span shorter than the lookback means the tag had no older "
                f"activity, not that collection failed.</p>"
            )
        body.append("</div>")

    if "narrative" in sections and narratives:
        body.append("<h2>Narrative volume</h2><div class='panel'>")
        body.append(_bar_chart([(n.label, n.post_count) for n in narratives]))
        body.append("</div>")

        body.append("<h2>Narrative detail</h2><div class='panel'><table>")
        body.append(
            "<tr><th>Narrative</th><th class='num'>Posts</th>"
            "<th class='num'>Comments</th><th class='num'>Engagement</th>"
            "<th class='num'>Share</th><th class='num'>Volume &Delta;</th>"
            "<th>Audience sentiment</th><th class='num'>Net</th></tr>"
        )
        for n in narratives:
            body.append(
                f"<tr><td>{_esc(n.label)}</td>"
                f"<td class='num'>{n.post_count:,}</td>"
                f"<td class='num'>{n.comment_count:,}</td>"
                f"<td class='num'>{n.total_engagement:,}</td>"
                f"<td class='num'>{n.share_of_voice * 100:.1f}%</td>"
                f"<td class='num'>{_fmt_delta(n.volume_delta_pct)}</td>"
                f"<td>{_sentiment_bar(n.comment_sentiment)}</td>"
                f"<td class='num'>{_fmt_net(n.comment_sentiment.net_sentiment)}</td>"
                "</tr>"
            )
        body.append("</table>")
        body.append(
            "<p class='note'>Engagement is absolute (likes + comments). "
            "Rates are not shown for the narrative lens because follower "
            "counts are not collected for non-allowlisted accounts.</p>"
        )
        body.append("</div>")

        if themes:
            by_key: dict[str, list[ThemeTerm]] = {}
            for term in themes:
                by_key.setdefault(term.narrative_key, []).append(term)
            label_of = {n.narrative_key: n.label for n in narratives}
            body.append("<h2>Language carrying each narrative</h2><div class='panel'>")
            for key, terms in by_key.items():
                body.append(
                    f"<p><strong>{_esc(label_of.get(key, key))}</strong><br>"
                    f"<span class='terms'>"
                    f"{_esc(', '.join(t.term for t in terms[:14]))}</span></p>"
                )
            body.append("</div>")

    # Evidence. A report with no examples cannot be checked.
    if samples and "narrative" in sections:
        label_of = {n.narrative_key: n.label for n in narratives}
        body.append("<h2>Sample posts</h2><div class='panel'>")
        body.append(
            "<p class='note'>Highest-engagement posts per narrative, so the "
            "numbers above can be checked against what was actually collected. "
            "Quoted posts are attributed so a citation can be verified; the "
            "wider corpus of authors and every commenter stays pseudonymous "
            "and is not tracked between runs.</p>"
        )
        for key, posts in samples.items():
            body.append(f"<h3>{_esc(label_of.get(key, key))}</h3>")
            for post in posts:
                caption = (post.get("caption") or "").strip()
                excerpt = (caption[:280] + "…") if len(caption) > 280 else caption
                body.append(
                    "<div class='sample'>"
                    f"<div class='sample-meta'>"
                    + (
                        f"<a href=\"https://www.instagram.com/"
                        f"{_esc(post['author_handle'])}/\" target=\"_blank\" "
                        f"rel=\"noopener noreferrer\">"
                        f"@{_esc(post['author_handle'])}</a> &middot; "
                        if post.get("author_handle") else ""
                    ) +
                    f"{post['posted_at']:%d %b %Y, %H:%M} &middot; "
                    f"{post['like_count'] or 0:,} likes &middot; "
                    f"{post['comment_count'] or 0:,} comments"
                    f"{' &middot; video' if post.get('is_video') else ''}"
                    f" &middot; <a href=\"{_esc(post['url'])}\" "
                    f"target=\"_blank\" rel=\"noopener noreferrer\">"
                    f"{_esc(post['post_shortcode'])}</a></div>"
                    f"<div class='sample-body'>"
                    f"{_esc(excerpt) or '<em>no caption</em>'}</div>"
                    "</div>"
                )
        body.append("</div>")

    if "public_figure" in sections:
        tracked = [f for f in figures if f.category != "own_side"]
        if tracked:
            body.append("<h2>Public figures</h2><div class='panel'><table>")
            body.append(
                "<tr><th>Account</th><th>Category</th>"
                "<th class='num'>Followers</th><th class='num'>Posts</th>"
                "<th class='num'>Avg engagement</th><th class='num'>Rate</th>"
                "<th>Audience sentiment</th></tr>"
            )
            for f in tracked:
                rate = (
                    f"{f.engagement_rate * 100:.2f}%"
                    if f.engagement_rate is not None else "n/a"
                )
                followers = f"{f.follower_count:,}" if f.follower_count else "n/a"
                body.append(
                    f"<tr><td>{_esc(f.display_name)}</td>"
                    f"<td><span class='tag'>"
                    f"{_esc(f.category.replace('_', ' '))}</span></td>"
                    f"<td class='num'>{followers}</td>"
                    f"<td class='num'>{f.post_count:,}</td>"
                    f"<td class='num'>{f.avg_engagement_per_post:,.0f}</td>"
                    f"<td class='num'>{rate}</td>"
                    f"<td>{_sentiment_bar(f.audience_sentiment)}</td></tr>"
                )
            body.append("</table></div>")

    if "own_side" in sections:
        own = [f for f in figures if f.category == "own_side"]
        if own:
            body.append("<h2>Own-side performance</h2><div class='panel'><table>")
            body.append(
                "<tr><th>Account</th><th class='num'>Followers</th>"
                "<th class='num'>Posts</th><th class='num'>Engagement</th>"
                "<th class='num'>Avg/post</th><th class='num'>Rate</th>"
                "<th>Audience sentiment</th></tr>"
            )
            for f in own:
                rate = (
                    f"{f.engagement_rate * 100:.2f}%"
                    if f.engagement_rate is not None else "n/a"
                )
                followers = f"{f.follower_count:,}" if f.follower_count else "n/a"
                body.append(
                    f"<tr><td>{_esc(f.display_name)}</td>"
                    f"<td class='num'>{followers}</td>"
                    f"<td class='num'>{f.post_count:,}</td>"
                    f"<td class='num'>{f.total_engagement:,}</td>"
                    f"<td class='num'>{f.avg_engagement_per_post:,.0f}</td>"
                    f"<td class='num'>{rate}</td>"
                    f"<td>{_sentiment_bar(f.audience_sentiment)}</td></tr>"
                )
            body.append("</table></div>")

    if provenance:
        body.append("<h2>Provenance</h2><div class='panel'><table>")
        body.append(
            f"<tr><th>Run</th><td class='mono'>#{provenance['id']} "
            f"({_esc(provenance['status'])})</td></tr>"
            f"<tr><th>Collected</th><td>"
            f"{provenance['started_at']:%d %b %Y, %H:%M} UTC</td></tr>"
            f"<tr><th>Config fingerprint</th>"
            f"<td class='mono'>{_esc(provenance['config_fingerprint'])}</td></tr>"
            f"<tr><th>Apify runs</th><td class='mono'>"
            f"{_esc(', '.join(provenance['actor_run_ids']) or 'none')}</td></tr>"
        )
        body.append("</table><p class='note'>Two runs are comparable only if "
                    "their config fingerprints match. Apify run IDs can be "
                    "opened in the console, or inspected with "
                    "<code>run.py debug-dataset</code>.</p></div>")

    body.append(
        "<h2>Scope</h2><div class='panel'><p class='note'>"
        "Accounts are named here only where they appear on the curated "
        "public-figure allowlist. All other authors contribute to aggregate "
        "counts under per-run pseudonyms and are not tracked between runs. "
        f"Sentiment scored by {_esc(cfg.settings.sentiment.actor_id)}; "
        f"{sentiment.uncertain:,} rows fell below the "
        f"{cfg.settings.sentiment.min_confidence:.2f} confidence threshold "
        "and are recorded as uncertain rather than assigned a polarity."
        "</p></div>"
    )
    body.append("</div>")

    document = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>Instagram Discourse Dashboard — "
        f"{_esc(cfg.settings.project.client_label)}</title>"
        f"<style>{_CSS}</style></head><body>{''.join(body)}</body></html>"
    )
    output_path.write_text(document, encoding="utf-8")
    logger.info("wrote %s", output_path)
    return output_path
