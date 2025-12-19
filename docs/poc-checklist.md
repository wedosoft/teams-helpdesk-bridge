# POC 체크리스트 (POSCO International Legal Help)

이 문서는 **Entra 앱 등록 + Azure Bot Service 등록이 완료된 상태**에서, POSCO International Legal Team POC를 돌리기 위해 필요한 **엔드포인트/설정/점검 항목**을 코드 기준으로 정리합니다.

## 1) 필수 엔드포인트(서버)

### Bot (Azure Bot Service → 우리 서버)
- 권장: `POST /api/bot/messages`
- 동일 기능: `POST /api/bot/callback`
- (옵션) `POST /api/messages` 별칭을 제공할 수 있음(배포 버전에 따라 존재)

Azure Bot Service의 **Messaging endpoint**는 다음 형태로 설정:
- `https://teams.wedosoft.net/api/bot/messages`

### Admin / Tenant 설정 (Teams Tab/관리자용)
- `GET /admin/setup` (관리자 설정 UI HTML)
- `GET /api/admin/config` (테넌트 설정 조회, 헤더 필요)
- `POST /api/admin/config` (테넌트 설정 저장, 헤더 필요)
- `GET /api/admin/validate` (플랫폼 연결 검증, 헤더 필요)
- `GET /api/admin/webhook-info` (웹훅 URL/가이드, 헤더 필요)

헤더:
- `X-Tenant-ID: <YOUR_TEAMS_TENANT_ID>`

### Helpdesk Webhook (Helpdesk → Teams 알림)
- Freshdesk(POC): `POST /api/webhook/freshdesk/{teams_tenant_id}`

> 참고: 코드베이스에는 Freshchat/Zendesk 경로도 남아있지만, **현재 Legal POC의 기준 플랫폼은 Freshdesk**입니다.

### 요청자(현업) 대시보드 (Freshdesk POC)
- `GET /tab/requests` (Teams Tab HTML)
- `GET /api/freshdesk/requests`
- `GET /api/freshdesk/requests/{ticket_id}`
- `POST /api/freshdesk/requests/{ticket_id}/inquiry`

헤더(POC 단계):
- `X-Tenant-ID: <YOUR_TEAMS_TENANT_ID>`
- `X-Requester-Email: <REQUESTER_EMAIL>`

## 2) 필수 환경변수(서버)

최소 필수:
- `PUBLIC_URL` (예: `https://teams-helpdesk-bridge.fly.dev`)
- `BOT_APP_ID`
- `BOT_APP_PASSWORD`
- `BOT_TENANT_ID` (단일 테넌트면 해당 tenant id 권장)
- `SUPABASE_URL`
- `SUPABASE_SECRET_KEY`
- `ENCRYPTION_KEY`

선택:
- `LOG_LEVEL` (기본 `info`)
- `OAUTH_REDIRECT_URI` (미설정 시 `PUBLIC_URL/admin/callback`로 계산)

## 3) Freshdesk POC 설정 순서(권장)

1. 테넌트 설정 저장 (`X-Tenant-ID` 필요)
   - `POST /api/admin/config`
   - payload 예시는 `docs/handover.md` 참고

2. 연결 검증
   - `GET /api/admin/validate`

3. Freshdesk Automation에서 Webhook 설정
   - URL: `https://<PUBLIC_URL>/api/webhook/freshdesk/<YOUR_TEAMS_TENANT_ID>`
   - payload는 최소 `ticket_id`, `text`(또는 `body`), `status`를 포함 권장

## 4) Teams 앱(매니페스트) 패키징

생성된 POC 패키지(예정):
- `teams-app/posco-legal-help-v1.0.9.zip` (manifest 내용이 바뀌었으므로 재패키징 필요)

포함 탭:
- “설정” → `https://teams.wedosoft.net/admin/setup`
- “내 요청함” → `https://teams.wedosoft.net/tab/requests`

## 5) 빠른 점검(증상별)

- Bot이 아예 호출되지 않음
  - Azure Bot Service Messaging endpoint가 `https://<PUBLIC_URL>/api/messages`인지 확인
  - `PUBLIC_URL`이 실제 외부 접근 가능한 HTTPS인지 확인

- `/api/admin/config`에서 “Failed to decrypt …” 계열 오류
  - `ENCRYPTION_KEY` 누락/불일치 (키 고정 후 재저장 필요)

- 요청자 대시보드가 “tenant/email을 가져오지 못했습니다” 표시
  - Teams Tab 컨텍스트를 못 받는 환경이면 쿼리로 테스트: `/tab/requests?tenant=...&email=...`
