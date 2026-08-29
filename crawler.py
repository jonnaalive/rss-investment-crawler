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
GEMINI_MODEL = "gemini-3.1-flash-lite"
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


def translate_summary(summary: str, api_key: str) -> str:
    if not summary or not api_key:
        return summary
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    prompt = (
        "다음 투자 뉴스 RSS 요약을 사실을 추가하거나 추론하지 말고 자연스러운 한국어 1~2문장으로 "
        "번역하라. 숫자, 단위, 회사명은 보존하고 번역문만 출력하라.\n\n" + summary[:1500]
    )
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.1, "maxOutputTokens": 300}}
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key, "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read())
    return clean_text(result["candidates"][0]["content"]["parts"][0]["text"])


def analyze_article(item: dict[str, Any], config: dict[str, Any], api_key: str) -> dict[str, Any]:
    if not api_key:
        return {}
    contexts = config.get("theme_context", {})
    related = list(item["companies"])
    context_lines = []
    for theme in item["themes"]:
        context = contexts.get(theme, {})
        related.extend(context.get("related_companies", []))
        context_lines.append(f"{theme}: {context.get('why_it_matters', '')}")
    related = list(dict.fromkeys(related))
    prompt = f"""당신은 보수적인 투자 뉴스 분석기다. 아래 RSS 정보만 사용하고 사실과 추론을 구분하라.
기사에 없는 실적 숫자, 목표가, 인과관계를 만들지 마라. 종목별 영향은 '가능한 시사점'으로만 쓰고,
관련성이 약하면 불명확이라고 답하라. 중요도는 포트폴리오 논지에 미칠 잠재 영향 기준 1~5다.

제목: {item['title']}
RSS 요약: {item['summary']}
분류: {', '.join(item['themes'])}
검토 종목: {', '.join(related) or '포트폴리오 전반'}
기존 연결 규칙: {' | '.join(context_lines)}

다음 JSON만 출력하라:
{{
  "translated_summary": "사실만 담은 자연스러운 한국어 1~2문장",
  "importance": 1,
  "importance_reason": "중요도 이유 한 문장",
  "implications": [
    {{"company": "티커", "direction": "긍정|부정|혼재|불명확", "text": "가능한 영향 한 문장"}}
  ],
  "watch_items": ["추가 확인 변수"],
  "caveat": "기사만으로 확정할 수 없는 점"
}}"""
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 900, "responseMimeType": "application/json"},
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key, "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        result = json.loads(response.read())
    raw = result["candidates"][0]["content"]["parts"][0]["text"].strip()
    analysis = json.loads(raw)
    analysis["importance"] = max(1, min(5, int(analysis.get("importance", 1))))
    analysis["implications"] = analysis.get("implications", [])[:5]
    analysis["watch_items"] = analysis.get("watch_items", [])[:3]
    return analysis


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
        analysis = item.get("analysis", {})
        summary = analysis.get("translated_summary") or item["summary"][:500] or "RSS에 별도 요약이 없습니다. 원문 확인이 필요합니다."
        importance = max(1, min(5, int(analysis.get("importance", 2))))
        bar = "■" * importance + "□" * (5 - importance)
        level_icon = {1: "⬜", 2: "🟩", 3: "🟨", 4: "🟧", 5: "🟥"}[importance]
        implications = analysis.get("implications", [])
        implication_text = "\n".join(
            f"- **{row.get('company', '?')} [{row.get('direction', '불명확')}]**: {row.get('text', '')}"
            for row in implications
        ) or "- 기사만으로 특정 종목 영향을 확정하기 어렵습니다."
        watch_text = " / ".join(analysis.get("watch_items", [])) or " / ".join(dict.fromkeys(check_parts)) or "원문 확인"
        lines.extend([
            "",
            f"### [{item['title']}]({item['url']})",
            f"**분류:** {' · '.join(item['themes']) or '공식 자료'}  |  **출처:** {item['source']}",
            f"**중요도:** {level_icon} `{bar}` **{importance}/5**",
            f"**중요도 근거:** {analysis.get('importance_reason', '공식 자료이지만 직접적인 종목 영향은 추가 확인이 필요합니다.')}",
            f"**무슨 일:** {summary}",
            f"**연결 종목:** {', '.join(related_companies) or '포트폴리오 전반'}",
            "**종목별 가능한 시사점:**",
            implication_text,
            f"**추가 확인:** {watch_text}",
            f"**해석 한계:** {analysis.get('caveat', 'RSS 요약만으로 실적 영향을 확정할 수 없습니다.')}",
            "**현재 판정:** REVIEW · 자동 매매 행동 없음",
        ])
    if errors:
        lines.extend(["", f"⚠️ 피드 오류 {len(errors)}건: " + ", ".join(errors)])
    return {"content": "\n".join(lines)[:1950]}


def post_discord(webhook: str, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(webhook, data=data, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status not in {200, 204}:
            raise RuntimeError(f"Discord HTTP {response.status}")


def run(dry_run: bool, send_test: bool, send_preview: bool = False) -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    webhook = os.environ.get("RSS_CRAWLER_WEBHOOK_URL", "").strip()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
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
    if not dry_run:
        for item in relevant:
            try:
                item["analysis"] = analyze_article(item, config, gemini_key)
            except (KeyError, IndexError, OSError, ValueError, urllib.error.URLError) as exc:
                print(f"AI 분석 실패, 규칙 기반 설명으로 대체: {item['id']} ({type(exc).__name__})", file=sys.stderr)
                try:
                    item["summary"] = translate_summary(item["summary"], gemini_key)
                except (KeyError, IndexError, OSError, ValueError, urllib.error.URLError):
                    pass

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
        for index, item in enumerate(relevant):
            post_discord(webhook, discord_payload([item], errors if index == 0 else [], config))

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
