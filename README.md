# fb-page-publisher

노션 회차 원장(DB-05)에서 **마스터가 승인한 회차 1건**을 Facebook 페이지에 **피드(사진+본문) + 릴스(영상)** 로 게시하고, 결과를 원장에 기록한다.

| 항목 | 값 |
|---|---|
| Runtime | Python 3.11 + GitHub Actions (ubuntu-latest) |
| Graph API | v25.0 (만료 2028-07-29) |
| Notion API | 2022-06-28 — **DB-05 에 데이터 소스를 추가하지 말 것** (추가 시 조회 실패) |
| 기본 모드 | DRY_RUN=true (실게시는 명시적 false 일 때만) |

## 구조

```
fbpub/
  settings.py       환경변수 → 상수, DB-05 속성명
  notion_repo.py    DB-05 조회·선점·결과 기록
  media_check.py    첨부 다운로드 + 이미지(4:5, 10MB)·영상(9:16, 3~90초, 24~60fps) 검사
  content_guard.py  고지문·3요건 줄·D+n·실제 날짜·반응 강요 문구 검사
  graph_client.py   photos / video_reels(start→rupload→finish→status)
  run_publish.py    오케스트레이터  (python -m fbpub.run_publish)
  preflight.py      토큰·권한·페이지·DB 스키마 점검 (python -m fbpub.preflight)
  notify.py         전용 Telegram 알림 (선택)
  redact.py         토큰·ID 마스킹
.github/workflows/
  publish.yml       매일 KST 20:05 + 랜덤 0~40분 / 수동 실행
  preflight.yml     매주 월 KST 09:17 / 수동 실행
  ci.yml            ruff + pytest
```

## 상태 전이 (DB-05 `게시상태`)

```
초안 → 검수중 → 승인 ─(Actions 선점)→ 발행중 → 발행완료
                                        ├→ 부분완료  (한쪽 채널만 성공 — "승인"으로 되돌리면 빈 채널만 재게시)
                                        ├→ 실패      (미게시 확정 / 사전 검사 불통과)
                                        └→ 확인필요  (응답 미확정 — 게시됐을 수 있음. 페이지 확인 후 수동 처리)
릴스 일일 상한 도달 시: 피드만 게시하고 "승인" 유지 → 다음 실행에서 릴스만 게시
```

운영 원칙: **Graph 쓰기 요청 재시도 금지**(중복 게시 방지), 응답 미확정은 `확인필요`로 멈춘다. 노션 호출만 429·5xx 시 최대 3회 재시도(멱등). 대상은 `게시상태=승인` **AND** `검수완료` 체크 **AND** 예약일시 도래(비어 있으면 즉시) 1건.
`발행중`으로 남은 행은 자동으로 다시 집지 않는다 (실행 중단·노션 장애 등). 매 실행 시작 시 잔류 행을 알림으로 보낸다.

### 수동 복구 절차 (확인필요 · 부분완료 · 발행중 잔류)

1. 결과코드 확인 — `*_id_unsaved=<ID>` 가 있으면 게시는 됐지만 원장 기록만 실패한 것. 해당 ID 를 FB피드ID/FB릴스ID 에 입력.
2. 페이지에서 실제 게시 여부 확인.
   - 게시됨 → ID 입력 후 `발행완료` 로 변경.
   - 게시 안 됨 → **해당 채널의 ID 칸을 비운 뒤** `승인` 으로 변경 (ID 가 남아 있으면 "이미 게시"로 간주되어 건너뜀).
3. 다음 실행에서 ID 가 빈 채널만 게시된다.

## 설정

### GitHub Secrets

| 이름 | 필수 | 내용 |
|---|---|---|
| `FBDET_PAGE_ID` | ✔ | Facebook 페이지 ID |
| `FBDET_PAGE_TOKEN` | ✔ | 장기 페이지 액세스 토큰 |
| `FBDET_NOTION_TOKEN` | ✔ | **전용** 노션 통합 토큰 (원장 DB 가 있는 영역에만 연결) |
| `FBDET_NOTION_DB_ID` | ✔ | DB-05 데이터베이스 ID |
| `FBDET_APP_ID` / `FBDET_APP_SECRET` | 선택 | preflight 의 debug_token 점검 |
| `FBDET_TELEGRAM_BOT_TOKEN` / `FBDET_TELEGRAM_CHAT_ID` | 선택 | 결과 알림 (전용 대화방. 다른 용도의 채널 ID 재사용 금지) |

### GitHub Variables

| 이름 | 기본 | 내용 |
|---|---|---|
| `FBDET_DRY_RUN` | (미설정 = true) | **`false` 로 등록하는 것이 운영 개시 스위치** (schedule 실행에 적용). 허용값 `true`/`false` 만 — 그 외 값은 설정 오류로 실행 중단 |
| `FBDET_REEL_AI_FLAG` | true | 릴스 finish 요청에 `is_ai_generated=true` 포함 |
| `FBDET_PHOTO_AI_NOTICE` | (없음) | 값이 있으면 피드 캡션 끝에 덧붙임 (예: AI 생성 이미지 표기) |

### Meta 준비

1. Meta 앱 생성 (기존 앱과 분리), 앱 역할에 본인 계정
2. 권한: `pages_show_list`, `pages_read_engagement`, `pages_manage_posts` (+ 페이지 CREATE_CONTENT 작업 권한)
3. 단기 사용자 토큰 → 장기 사용자 토큰 교환 (`/oauth/access_token?grant_type=fb_exchange_token`, 서버에서만) → `/{user-id}/accounts` 에서 장기 페이지 토큰 획득
4. Secrets 등록 → `fb-preflight` 수동 실행으로 확인
5. App Review 필요 여부는 공식 문서 간 서술이 다르므로 preflight + DRY_RUN + 실게시 1회로 실측

## 실행

```bash
pip install -r requirements.txt -r requirements-dev.txt
ruff check . && python -m pytest -q          # 배포 전 각 2회 연속 통과
DRY_RUN=true python -m fbpub.run_publish     # 노션 읽기 + 검사만, 쓰기 없음
python -m fbpub.preflight
```

종료코드: run_publish `0` 정상(대상 없음·DRY_RUN 통과·발행완료·상한 이월) / `2` 실패·확인필요·부분완료 / `1` 설정 누락.

## 운영 개시 순서

1. 노션 DB-05 준비 + 전용 통합 연결 → Secrets 등록
2. `fb-preflight` 수동 실행 → 전부 통과
3. 테스트 행 1건 승인 → `fb-publish` 수동 실행 `dry_run=true` → 로그 확인
4. 마스터 입회 실게시 1회 (`dry_run=false`) → 피드·릴스·AI 표시·원장 ID 확인
5. Variables `FBDET_DRY_RUN=false` → 예약 실행 개시
