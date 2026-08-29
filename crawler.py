#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "data" / "state.json"
USER_AGENT = "rss-investment-crawler/1.0 (personal investment monitor)"
TAG_RE = re.compile(r"<[^>]+>")


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(html.unescape(TAG_RE.sub(" ", value)).split())


def child_text(node: ET.Element, names: tuple[str, ...]) -> str:
    for child in node.iter():
        local = child.tag.rsplit("}", 1)[-1].lower()
        if local in names and child.text:
            return clean_text(child.text)
    return ""


def parse_feed(payload: bytes, feed: dict[str, Any]) -> list[dict[str, str]]:
    root = ET.fromstring(payload)
    nodes = [n for n in root.iter() if n.tag.rsplit("}", 1)[-1].lower() in {"item", "entry"}]
    articles: list[dict[str, str]] = []
    for node in nodes:
        title = child_text(node, ("title",))
        summary = child_text(node, ("description", "summary", "content"))
        guid = child_text(node, ("guid", "id"))
        published = child_text(node, ("pubdate", "published", "updated", "date"))
        link = child_text(node, ("link",))
        if not link:
            for child in node.iter():
                if child.tag.rsplit("}", 1)[-1].lower() == "link" and child.attrib.get("href"):
                    link = child.attrib["href"]
                    break
        if not title or not link:
            continue
        identity = guid or link
        article_id = hashlib.sha256(f"{feed['id']}:{identity}".encode()).hexdigest()
        link = urllib.parse.urljoin(feed.get("url", ""), link)
        articles.append({
            "id": article_id,
            "feed_id": feed["id"],
            "source": feed["name"],
            "title": title,
            "summary": summary,
            "url": link,
            "published_at": published,
        })
    return articles


def fetch_feed(feed: dict[str, Any]) -> list[dict[str, str]]:
    request = urllib.request.Request(feed["url"], headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=12) as response:
        return parse_feed(response.read(), feed)


def classify(article: dict[str, str], config: dict[str, Any], source_weight: int) -> dict[str, Any]:
    text = f"{article['title']} {article['summary']}".casefold()
    themes = [name for name, words in config["themes"].items() if any(word.casefold() in text for word in words)]
    companies = [ticker for ticker, aliases in config["companies"].items() if any(alias.casefold() in text for alias in aliases)]
    score = source_weight + min(4, len(themes) * 2) + (5 if companies else 0)
    return {**article, "themes": themes, "companies": companies, "score": score}


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"initialized": False, "seen": {}, "feed_health": {}}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def prune_seen(seen: dict[str, str], days: int) -> dict[str, str]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = {}
    for key, value in seen.items():
        try:
            if datetime.fromisoformat(value) >= cutoff:
                result[key] = value
        except ValueError:
            continue
    return result


def discord_payload(
    items: list[dict[str, Any]],
    errors: list[str],
    config: dict[str, Any] | None = None,
    test: bool = False,
) -> dict[str, Any]:
    if test:
        return {"content": "✅ RSS crawler 연결 테스트: `rss-crawler` 채널 Webhook이 정상입니다."}
    lines = [f"**📡 투자 RSS 업데이트: {len(items)}건**"]
    for item in items:
        contexts = (config or {}).get("theme_context", {})
        related_companies = list(item["companies"])
        why_parts: list[str] = []
        check_parts: list[str] = []
        for theme in item["themes"]:
            context = contexts.get(theme, {})
            related_companies.extend(context.get("related_companies", []))
            if context.get("why_it_matters"):
                why_parts.append(context["why_it_matters"])
            if context.get("check"):
                check_parts.append(context["check"])
        related_companies = list(dict.fromkeys(related_companies))
        summary = item["summary"][:500] or "RSS에 별도 요약이 없습니다. 원문 확인이 필요합니다."
        lines.extend([
            "",
            f"### [{item['title']}]({item['url']})",
            f"**분류:** {' · '.join(item['themes']) or '공식 자료'}  |  **출처:** {item['source']}",
            f"**무슨 일:** {summary}",
            f"**왜 보나:** {' '.join(dict.fromkeys(why_parts)) or '보유 종목 직접 언급 여부와 투자 논지 영향을 확인해야 합니다.'}",
            f"**연결 종목:** {', '.join(related_companies) or '포트폴리오 전반'}",
            f"**확인 행동:** {' / '.join(dict.fromkeys(check_parts)) or '원문 확인 후 논지 변화 여부 판정'}",
            "**현재 판정:** REVIEW · 자동 매매 행동 없음",
        ])
    if errors:
        lines.extend(["", f"⚠️ 피드 오류 {len(errors)}건: " + ", ".join(errors)])
    return {"content": "\n".join(lines)[:1900]}


def post_discord(webhook: str, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(webhook, data=data, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status not in {200, 204}:
            raise RuntimeError(f"Discord HTTP {response.status}")


def run(dry_run: bool, send_test: bool, send_preview: bool = False) -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    webhook = os.environ.get("RSS_CRAWLER_WEBHOOK_URL", "").strip()
    if send_test:
        if not webhook:
            print("RSS_CRAWLER_WEBHOOK_URL이 필요합니다.", file=sys.stderr)
            return 2
        post_discord(webhook, discord_payload([], [], test=True))
        print("Discord 테스트 메시지를 전송했습니다.")
        return 0

    state = load_state()
    now = datetime.now(timezone.utc).isoformat()
    all_articles: list[dict[str, Any]] = []
    errors: list[str] = []
    feed_weights = {feed["id"]: feed["source_weight"] for feed in config["feeds"]}
    for feed in config["feeds"]:
        try:
            articles = fetch_feed(feed)
            all_articles.extend(articles)
            state["feed_health"][feed["id"]] = {"last_success": now, "items": len(articles)}
        except (OSError, urllib.error.URLError, ET.ParseError) as exc:
            errors.append(feed["id"])
            state["feed_health"][feed["id"]] = {"last_error": now, "error": str(exc)[:200]}

    unseen = [article for article in all_articles if send_preview or article["id"] not in state["seen"]]
    relevant = [
        classify(article, config, feed_weights[article["feed_id"]])
        for article in unseen
    ]
    relevant = [item for item in relevant if item["score"] >= config["minimum_score"]]
    relevant.sort(key=lambda item: (item["score"], item["published_at"]), reverse=True)
    relevant = relevant[: config["max_items_per_message"]]

    if send_preview:
        if not webhook:
            print("RSS_CRAWLER_WEBHOOK_URL이 필요합니다.", file=sys.stderr)
            return 2
        if not relevant:
            print("미리보기로 보낼 관련 기사가 없습니다.")
            return 1
        post_discord(webhook, discord_payload(relevant[:1], errors, config))
        print(f"미리보기 1건을 전송했습니다: {relevant[0]['title']}")
        return 0

    if dry_run:
        print(json.dumps({"fetched": len(all_articles), "unseen": len(unseen), "relevant": relevant, "errors": errors}, ensure_ascii=False, indent=2))
        return 0 if not errors else 1

    first_run = not state.get("initialized", False)
    if not first_run and relevant:
        if not webhook:
            print("RSS_CRAWLER_WEBHOOK_URL이 없어 발송하지 못했습니다.", file=sys.stderr)
            return 2
        post_discord(webhook, discord_payload(relevant, errors, config))

    state["initialized"] = True
    state["last_run"] = now
    state["seen"] = prune_seen({**state["seen"], **{a["id"]: now for a in all_articles}}, config["state_retention_days"])
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"수집 {len(all_articles)}건, 신규 {len(unseen)}건, 관련 {len(relevant)}건, 최초실행={first_run}")
    return 0 if all_articles else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--send-test", action="store_true")
    parser.add_argument("--send-preview", action="store_true")
    args = parser.parse_args()
    raise SystemExit(run(args.dry_run, args.send_test, args.send_preview))
