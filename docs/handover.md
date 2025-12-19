# Teams Helpdesk Bridge - 인수인계 (Handover)

## 프로젝트 개요

Microsoft Teams와 헬프데스크 플랫폼(Freshchat/Zendesk/Freshdesk) 간 양방향 메시지 브릿지 서비스.

- **배포 URL**: https://teams.wedosoft.net
- **플랫폼**: Fly.io (512MB RAM)
- **데이터베이스**: Supabase (PostgreSQL + Storage)

---

## 최근 완료된 작업 (2025-12-19) - POSCO “Legal Help” POC (Freshdesk)

### 1) POC 아키텍처/런북 문서화

- `docs/posco/posco-poc-architecture.md`: POC 아키텍처/흐름 정리
- `docs/posco/posco-poc-runbook.md`: 로컬/배포 환경에서 재현 가능한 실행 가이드
- `docs/posco/posco-poc-BS.md`: 요구사항/정책/의사결정(가중치 등) 의견 정리
- 원문 이메일 컨텍스트: `docs/posco/content.txt`, `docs/posco/content.pdf`

### 2) Freshdesk 연동(POC 기준) 구현

Freshdesk를 **SSOT(진행/상태/답변)**로 사용하고, Teams는 인테이크(접수) + 대시보드(조회) + 문의(공개 메모) UX를 제공.

- Freshdesk 클라이언트: `app/adapters/freshdesk/client.py`
  - 인증: Basic Auth (`username=api_key`, `password='X'`)
  - 주요 기능: 티켓 생성/조회/목록, 문의(공개 메모) 추가, 연결 검증
  - 제한(POC): **바이너리 첨부 업로드는 미지원**(Teams에서는 링크 첨부 권장)
- 웹훅 라우터/핸들러: `app/adapters/freshdesk/routes.py`, `app/adapters/freshdesk/webhook.py`
- 요청자(팀즈 탭)용 API:
  - `GET /api/freshdesk/requests` (내 티켓 목록)
  - `GET /api/freshdesk/requests/{ticket_id}` (티켓 상세)
  - `POST /api/freshdesk/requests/{ticket_id}/inquiry` (문의 추가 = 공개 메모)
  - 구현: `app/adapters/freshdesk/requester_routes.py`
  - 주의: 위 API는 PoC 단계에서 **헤더 기반 식별**을 사용함
    - `X-Tenant-ID`: Teams 테넌트 ID
    - `X-Requester-Email`: 요청자 이메일
    - 상세/문의 API는 최소한의 소유권 체크가 있어, 이메일이 티켓 requester와 다르면 403이 발생할 수 있음
- 요청자 대시보드(Teams Tab HTML):
  - UI: `app/static/requests.html`
  - 라우팅: `app/main.py`에서 `/tab/requests` 제공

### 3) Teams 인테이크 카드(“검토요청”) 구현

- 봇 채팅에서 `검토요청` 입력 → Adaptive Card 인테이크 폼 응답
- 제출(invoke) → Freshdesk 티켓 생성 호출 → 케이스 번호 안내
- 구현 파일:
  - `app/teams/bot.py` (invoke 처리 포함)
  - `app/core/router.py` (명령 처리/라우팅)

> 정책: 가중치는 요청자가 입력하지 않고 **법무팀이 Freshdesk 커스텀필드로 부여**하는 방식(A안)으로 정리.

### 4) 테넌트 설정/암호화/환경변수 정리

- 테넌트별 플랫폼 자격증명은 **Supabase `tenants.platform_config`에 암호화 저장** (서버 환경변수에 Freshdesk API Key를 하드코딩하지 않음).
  - 암복호화 유틸: `app/utils/crypto.py`
- Supabase 프로젝트에서 legacy 키(anon/service_role)가 비활성화된 경우를 대비하여
  - 서버는 `SUPABASE_SECRET_KEY` 사용: `app/config.py`, `app/database.py`
- `ENCRYPTION_KEY`는 **필수** (누락/변경 시 기존에 저장된 암호화 설정을 복호화할 수 없음)
  - 주의사항/키 생성법을 런북에 명시: `docs/posco/posco-poc-runbook.md`
- `.env.example`에서 혼동되는 미사용 변수(Freshchat/Zendesk 관련 샘플) 정리: `.env.example`

### 5) /api/admin/validate 디버깅 UX 개선

- 복호화 실패/환경변수 누락 같은 RuntimeError를 5xx로만 내보내지 않고 JSON으로 반환하여 PoC 디버깅을 쉽게 함
- 구현: `app/admin/routes.py`

---

## POC 운영/테스트 체크리스트 (Freshdesk 기준)

### 필수 환경변수

로컬/배포 공통(값은 절대 커밋 금지):
- `PUBLIC_URL`
- `BOT_APP_ID`, `BOT_APP_PASSWORD`, `BOT_TENANT_ID` (Azure AD App/Bot 설정)
- `SUPABASE_URL`, `SUPABASE_SECRET_KEY`
- `ENCRYPTION_KEY`
- `LOG_LEVEL` (선택)

### 로컬 실행

```bash
python3 -m venv venv
source venv/bin/activate
python3 -m pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 테넌트 설정(로컬 POC)

Teams SSO 없이도 테스트할 수 있도록 `X-Tenant-ID` 헤더를 지원.

1) 설정 저장:
```bash
curl -X POST 'http://localhost:8000/api/admin/config' \
  -H 'Content-Type: application/json' \
  -H 'X-Tenant-ID: <YOUR_TEAMS_TENANT_ID>' \
	  -d '{
	    "platform": "freshdesk",
	    "freshdesk": {
	      "base_url": "https://<YOUR_DOMAIN>.freshdesk.com",
	      "api_key": "<FRESHDESK_API_KEY>",
	      "weight_field_key": "cf_weight"
	    },
	    "bot_name": "Legal Help",
	    "welcome_message": "접수되었습니다. 담당자가 확인 후 답변드립니다."
	  }'
```

> 참고: Freshdesk POC 경로에서는 첫 인사 메시지를 `welcome_message` 대신 케이스 번호 포함 형태로 고정해서 보내고 있습니다. (`app/core/router.py`)

2) 연결 검증:
```bash
curl 'http://localhost:8000/api/admin/validate' -H 'X-Tenant-ID: <YOUR_TEAMS_TENANT_ID>'
```

### 요청자 대시보드(로컬)

Teams 탭에서 SSO 붙이기 전 POC 단계에서는 다음 형태로도 확인 가능:
- `/tab/requests?tenant=<TENANT_ID>&email=<REQUESTER_EMAIL>`

### 자주 겪는 이슈/해결

- `Failed to decrypt ...` / `Freshdesk config missing`
  - 원인: `ENCRYPTION_KEY` 누락 또는 저장 당시 키와 불일치
  - 해결: `ENCRYPTION_KEY`를 고정(환경별 동일)하고, 필요 시 `POST /api/admin/config`로 재저장(재암호화)
- Supabase “Legacy API keys are disabled”
  - 원인: legacy(anon/service_role) 키 비활성화
  - 해결: `SUPABASE_SECRET_KEY`를 사용
- Teams에서 실제로 호출이 안됨
  - 원인: `PUBLIC_URL`이 로컬(`http://localhost`)이거나, Bot 등록 정보(Azure)와 매니페스트가 불일치
  - 해결: Fly 등 외부 공개 URL로 배포 후 `PUBLIC_URL`/Teams manifest를 맞춤

### 배포(Fly.io)

```bash
fly deploy
fly logs -a teams-helpdesk-bridge
```

운영 시 Fly Secrets에 들어가야 하는 값(예시):
- `PUBLIC_URL`
- `BOT_APP_ID`, `BOT_APP_PASSWORD`, `BOT_TENANT_ID`
- `SUPABASE_URL`, `SUPABASE_SECRET_KEY`
- `ENCRYPTION_KEY`
- `LOG_LEVEL`

---

## 알려진 제한사항 / TODO

- 요청자 대시보드 인증은 PoC 단계에서 단순화되어 있음(Teams SSO 연동은 후속 작업)
- Supabase CLI의 일부 기능(`supabase db dump` 등)은 Docker 데몬이 필요할 수 있음(로컬 개발 환경에 따라 차이)
- 봇 토큰 발급 401이 보이면(`login.microsoftonline.com ... 401`) Bot App 비밀번호/테넌트 설정을 우선 점검

---

## 이전 작업 기록 (2024-11-30)

### 1. 첨부파일 통합 전송

**문제**: 텍스트와 첨부파일이 별도 메시지로 전송되어 대화 흐름이 끊김

**해결**: `_send_combined_message_to_teams` 메서드 구현
- 텍스트 + 이미지 + 비디오/파일을 하나의 메시지로 통합
- 이미지: Adaptive Card로 인라인 표시
- 비디오: 🎬 마크다운 링크
- 파일: 📎 마크다운 링크

**파일**: [app/core/router.py](../app/core/router.py) - `_send_combined_message_to_teams()`

---

### 2. 첨부파일 병렬 업로드 최적화

**문제**: 순차적 API 호출로 메시지 전송 지연

**해결**: `asyncio.gather()` 활용한 병렬 처리
- Teams → Freshchat 이미지 전송 시 Supabase + Freshchat 동시 업로드
- 여러 첨부파일도 병렬 처리

**파일**: [app/core/router.py](../app/core/router.py)
- `_process_attachment_parallel()` - 단일 첨부파일 병렬 처리
- `_process_attachments_parallel()` - 다중 첨부파일 병렬 처리

---

### 3. 이미지 표시 개선 (HeroCard → Adaptive Card)

**문제**: HeroCard 사용 시 이미지가 카드 너비에 맞춰 늘어남 (비율 깨짐)

**해결**: Adaptive Card + Image 요소 사용
```json
{
  "type": "Image",
  "url": "...",
  "size": "Medium",
  "selectAction": {
    "type": "Action.OpenUrl",
    "url": "원본 이미지 URL"
  }
}
```
- `size: "Medium"`: 적절한 크기로 제한 (비율 유지)
- `selectAction`: 클릭 시 원본 이미지 열기

**파일**: [app/core/router.py](../app/core/router.py)
- `_send_combined_message_to_teams()`
- `_send_attachments_to_teams()`

---

### 4. 한글 파일명 업로드 오류 수정

**문제**: Supabase Storage가 비-ASCII 파일명 거부

**해결**: UUID 기반 파일명으로 대체
```python
file_path = f"{uuid.uuid4().hex[:12]}{ext}"
```

**파일**: [app/database.py](../app/database.py) - `upload_to_storage()`

---

### 5. 클립보드/스크린샷 이미지 처리

**문제**: Teams에서 붙여넣기한 이미지가 Freshchat에 전송 안됨

**해결**: `text/html` 첨부파일에서 `<img src>` URL 추출
```python
img_urls = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html_content)
```

**파일**: [app/teams/bot.py](../app/teams/bot.py) - `_parse_attachments()`

---

## 아키텍처 요약

```
Teams User
    ↓
TeamsBot (app/teams/bot.py)
    ↓
MessageRouter (app/core/router.py)
    ↓
PlatformFactory (app/core/platform_factory.py)
    ↓
FreshchatClient / ZendeskClient / FreshdeskClient (app/adapters/)
```

### 주요 캐싱

| 항목 | TTL | 위치 |
|------|-----|------|
| Platform Client | 10분 | `PlatformFactory._cache` |
| Agent 정보 | 30분 | `FreshchatClient._agent_cache`, `ZendeskClient._agent_cache`, `FreshdeskClient._agent_cache` |
| Supabase Client | 영구 | `@lru_cache` |

---

## 주요 파일 설명

| 파일 | 설명 |
|------|------|
| `app/core/router.py` | 메시지 라우팅 핵심 로직 |
| `app/teams/bot.py` | Teams Bot Framework 핸들러 |
| `app/adapters/freshchat/client.py` | Freshchat API 클라이언트 |
| `app/adapters/freshchat/webhook.py` | Freshchat 웹훅 파서 |
| `app/adapters/freshdesk/client.py` | Freshdesk API 클라이언트(POC: 티켓/노트) |
| `app/adapters/freshdesk/routes.py` | Freshdesk 웹훅 라우터 |
| `app/adapters/freshdesk/webhook.py` | Freshdesk 웹훅 파서 |
| `app/adapters/freshdesk/requester_routes.py` | Freshdesk 요청자(Teams 탭) API |
| `app/database.py` | Supabase DB/Storage 클라이언트 |
| `app/core/platform_factory.py` | 플랫폼 클라이언트 팩토리 |
| `app/core/tenant.py` | 멀티테넌트 설정 관리 |

---

## 배포

```bash
# Fly.io 배포
fly deploy

# 로그 확인
fly logs -a teams-helpdesk-bridge
```

---

## 알려진 제한사항

1. **메모리**: 512MB - 대용량 파일 처리 시 주의
2. **Freshchat 파일 업로드**: 이미지는 `image` 타입, 기타는 `file` 타입 사용 필요
3. **Teams Adaptive Card**: 버전 1.4 사용 중
4. **Freshdesk(POC) 첨부**: 바이너리 업로드는 미지원(링크 첨부 권장)

---

## 향후 개선 가능 항목

- [ ] 요청자 대시보드 인증(Teams SSO) 적용 및 헤더 기반 식별 제거
- [ ] Zendesk 웹훅 시크릿(서명 검증) 테넌트 설정 연동 (`app/adapters/zendesk/routes.py` TODO)
- [ ] Freshdesk 바이너리 첨부 업로드 지원(또는 링크 기반 UX 고도화)
- [ ] Freshdesk 첫 인사 메시지 정책 정리(케이스 번호 + 커스텀 welcome_message 조합 등)
- [ ] 에러 재시도/백오프 및 모니터링/알림 강화
