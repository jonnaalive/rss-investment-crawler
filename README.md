# RSS Investment Crawler

보유 종목과 핵심 투자 테마에 관련된 공식 RSS만 수집해 Discord `rss-crawler` 채널로 보냅니다.

## 범위

- EIA: Today in Energy, Press Releases
- Federal Reserve: Monetary Policy, Industrial Production, SLOOS
- Guardian, Ars, SEC 일반 보도자료는 제외
- SEC 가이던스 변화는 기존 `guidance-discord-bot`이 담당

GitHub Actions는 하루 4회 실행합니다. 신규 기사 중 투자 키워드와 일치한 항목만 전송하며, 첫 실행은 기존 항목을 기준선으로 등록하고 발송하지 않습니다.

## Discord 연결

1. Discord `rss-crawler` 채널 설정에서 `연동` → `웹후크` → `새 웹후크`를 만듭니다.
2. 이 프로젝트의 GitHub 저장소에서 `Settings` → `Secrets and variables` → `Actions`로 이동합니다.
3. `RSS_CRAWLER_WEBHOOK_URL`이라는 이름으로 Webhook URL을 저장합니다.
4. 한국어 번역용 Gemini 키를 `GEMINI_API_KEY`라는 이름으로 저장합니다.

Webhook URL과 Gemini 키는 파일이나 Git 커밋에 넣지 않습니다. 관련 기사로 선별된 항목의 RSS 요약만 Gemini Free Tier로 번역하며, 번역 실패 시 영어 원문으로 대체합니다.

## 로컬 검증

```bash
python3 crawler.py --dry-run
python3 -m unittest discover -s tests -v
```

`--dry-run`은 Discord로 전송하거나 `data/state.json`을 수정하지 않습니다. 최초 실제 실행은 기준선만 만들며, 테스트 알림은 Actions의 `workflow_dispatch`에서 `send_test`를 선택해 보낼 수 있습니다.
`send_preview`는 기준선을 수정하지 않고 현재 관련 기사 중 1건만 실제 형식으로 전송합니다.

## 설정

- 피드와 테마: `config.json`
- 실행 상태: `data/state.json`
- 스케줄: `.github/workflows/crawl.yml`

새 피드를 추가할 때는 공식 RSS만 사용합니다. 일반 뉴스 전체 피드는 추가하지 않습니다.
