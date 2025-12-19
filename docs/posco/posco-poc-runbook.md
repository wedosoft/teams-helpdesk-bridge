# POSCO “Legal Help” POC 실행 가이드 (이 저장소 기준)

이 문서는 `teams-helpdesk-bridge` 백엔드에서 **Freshdesk(Omni) 티켓 기반**으로 POC를 빠르게 재현하기 위한 런북입니다.

---

## 1) 사전 준비물

### (A) Teams Bot(Azure AD App)

- Bot App ID / Password
- (선택) 테넌트 제한이 필요하면 `BOT_TENANT_ID` 설정

### (B) Supabase

- `supabase/migrations/001_initial_schema.sql` 실행
- `SUPABASE_URL`, `SUPABASE_SECRET_KEY` 준비 (프로젝트에서 legacy 키가 비활성화된 경우 필수)

### (C) Freshdesk(Omni)

- Freshdesk 포털 Base URL (예: `https://<domain>.freshdesk.com`)
- API Key
- (권장) 가중치 커스텀 필드 생성 후 키 확보 (예: `cf_weight`)

---

## 2) 환경변수 설정

`.env`를 준비합니다. 샘플은 `.env.example` 참고.

필수:
- `PUBLIC_URL` (Webhook/Teams에서 접근 가능한 URL)
- `BOT_APP_ID`, `BOT_APP_PASSWORD`
- `SUPABASE_URL`, `SUPABASE_SECRET_KEY`
- `ENCRYPTION_KEY` (운영 기준 필수)

로컬 개발도 Supabase(클라우드)를 사용합니다.
- 로컬에 별도 DB를 띄우지 않고, `.env.local`에 Supabase 접속 정보를 넣어 사용합니다.

### ENCRYPTION_KEY 주의사항

- `.env.local`에서 **값 뒤에 인라인 코멘트(예: `ENCRYPTION_KEY=xxx # comment`)를 붙이지 마세요.** 파서에 따라 코멘트가 값으로 들어가 복호화가 실패할 수 있습니다.
- `ENCRYPTION_KEY`는 **테넌트 설정 저장 시점에 암호화에 사용**되며, 이후 복호화에도 동일 키가 필요합니다.
  - 이미 다른 키로 저장해 둔 테넌트가 있다면 `POST /api/admin/config`를 다시 호출해서 **재저장(재암호화)** 해야 합니다.

키 생성 예시(둘 중 하나):

```bash
python3 -c 'import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'
```

```bash
openssl rand -base64 32 | tr -d '\n'
```

---

## 3) 서버 실행

```bash
python3 -m venv venv
source venv/bin/activate
python3 -m pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 4) 테넌트 설정(Freshdesk) 저장

POC/로컬에서는 Teams SSO 대신 `X-Tenant-ID` 헤더로 설정할 수 있습니다.

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

연결 검증:

```bash
curl 'http://localhost:8000/api/admin/validate' -H 'X-Tenant-ID: <YOUR_TEAMS_TENANT_ID>'
```

---

## 5) Teams에서 인테이크 카드(검토요청) 사용

- 봇과의 채팅에서 `검토요청` 입력
- Adaptive Card 폼 작성 후 **접수하기** 클릭
- 성공 시 Teams에 “케이스 번호”가 안내됩니다.

> 가중치(1~5)는 **요청자가 입력하지 않고**, 법무팀이 Freshdesk에서 커스텀 필드로 부여하는 흐름을 권장합니다.
> `weight_field_key`는 대시보드 집계(가중치 합산)를 위해 설정해 두면 됩니다.

---

## 6) Freshdesk → Teams 알림(웹훅) 테스트

Freshdesk Automation에서 webhook을 설정하기 전, 로컬에서 먼저 형태를 고정하고 테스트하는 것을 권장합니다.

예시 payload:

```bash
curl -X POST 'http://localhost:8000/api/webhook/freshdesk/<YOUR_TEAMS_TENANT_ID>' \
  -H 'Content-Type: application/json' \
  -d '{
    "ticket_id": 123,
    "text": "추가 자료 부탁드립니다.",
    "status": 3,
    "actor_type": "agent",
    "actor_id": "999"
  }'
```

전제 조건:
- `ticket_id=123`이 **Teams에서 생성된 케이스**여야 매핑이 존재하여 Proactive 알림이 전송됩니다.

---

## 6.5) 요청자 대시보드(Teams 탭) 빠른 확인

요청자(현업)가 Teams에서 “내 요청함”을 확인하고, 진행 중 티켓에 **문의(공개 메모)**를 추가할 수 있는 최소 UI가 포함되어 있습니다.

- 탭 URL: `/tab/requests`
- 사용 API:
  - `GET /api/freshdesk/requests` (내 티켓 목록)
  - `GET /api/freshdesk/requests/{ticket_id}` (티켓 상세)
  - `POST /api/freshdesk/requests/{ticket_id}/inquiry` (문의 추가 = 공개 메모)

POC 단계에서는 Teams SSO 대신 아래 헤더로 요청자를 식별합니다.
- `X-Tenant-ID`: Teams tenant id
- `X-Requester-Email`: 요청자 이메일

로컬 브라우저에서 Teams 없이 확인하려면 URL 쿼리를 사용합니다.
- `/tab/requests?tenant=<TENANT_ID>&email=<REQUESTER_EMAIL>`

---

## 7) POC 데모 시나리오(권장 멘트)

1. “Teams에서 검토요청 등록(가중치 포함) → 케이스 번호가 즉시 발급됩니다.”
2. “법무는 Freshdesk에서 배정/상태/코멘트를 남기고, 변경사항이 Teams로 알림됩니다.”
3. “가중치 필드를 기반으로 단순 건수가 아닌 ‘실질 업무량’ 대시보드까지 확장 가능합니다.”

---

## (부록) 간단 대시보드 API(POC)

Freshdesk 티켓을 단순 집계하는 API가 포함되어 있습니다.

```bash
curl 'http://localhost:8000/api/admin/freshdesk/dashboard?per_page=100' \
  -H 'X-Tenant-ID: <YOUR_TEAMS_TENANT_ID>'
```
