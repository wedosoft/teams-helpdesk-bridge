# POSCO 법무실 “Legal Help” POC 아키텍처 검토 (Backend 중심)

## 0) 원문 요구사항 요약 (2025-12-17~18 메일)

메일([content.txt](content.txt)) 기준으로 고객이 원하는 핵심은 아래 5가지로 정리됩니다.

1. **채널 전환 최소화**: 법무앱을 폐쇄하고 **Outlook/Teams 기반**으로 진행
2. **케이스(검토건) 원장화**: Teams/메일로 주고받는 건도 “진행건/완료건”으로 **등록·추적**
3. **상위자 가시성**: 진행/완료/총건수, 실원별 처리량 확인
4. **가중치(1~5)**: 단순 건수 외에 난이도/중요도 기반의 “업무량” 지표 필요
5. **운영 편의**: (i) 담당자가 Teams/Outlook에서 **클릭/등록**하거나 (ii) 현업이 직접 등록하고 그룹장이 배정

추가로 “열람자(CC) 추가”, “요청자가 진행상황 확인”, “대화/메일 내용을 담당자가 시스템에 올릴 수 있나”가 포함됩니다.

---

## 1) 기술적 타당성 결론

### 가능한 것 (POC 수준에서 충분히 가능)

- **Teams Bot + Adaptive Card**로 “검토요청 등록(제목/내용/가중치/열람자/첨부링크)” 입력 UX 구현
- 등록 시 **Freshdesk(Omni) 티켓 생성** + 티켓ID를 Teams에 즉시 회신
- Freshdesk에서 상태변경/노트/답변 발생 시 **Webhook → Bridge → Teams Proactive 메시지**로 알림
- 상위자용 지표는 **Teams Tab(웹뷰)** 형태로 “실원별 진행중/완료/가중치합” 최소 대시보드 제공 가능

### 한계/주의사항 (처음부터 기대치 조정 필요)

- **Teams ‘채팅창만’으로** 목록/검색/통계까지 해결하기는 구조적으로 어렵고, **Tab(웹 UI)**가 필요합니다.
- Outlook “메일을 클릭해서 등록”은
  - (A) **전달(Forwarding) 기반**(가장 단순) 또는
  - (B) **Outlook Add-in 개발**(POC 난이도↑)
  로 갈립니다. POC는 A가 안정적입니다.
- Freshdesk에서 “사용자 메시지처럼” 남기는 완벽한 대화 동기화는 제품/권한/이메일 알림 정책 영향을 받습니다.
  - POC에서는 **‘등록/상태/알림’** 위주로 성공시키고, 대화 동기화는 범위를 축소하는 게 안전합니다.

---

## 2) 권장 POC 아키텍처 (이 저장소 기준)

### SSOT(원장) 선택

POC에서는 **Freshdesk(Omni) 티켓 = 케이스 원장(SSOT)** 으로 두는 구성이 가장 빠릅니다.

- Teams: 인테이크(등록) + 알림 + 간단 질의응답
- Bridge Server(이 저장소): Teams ↔ Freshdesk API/Webhook 중계 + 매핑/권한 최소
- Freshdesk: 케이스(티켓) 원장 + 배정/상태/이력/리포팅

### 기존 백엔드와의 정합성

현재 저장소는 이미 아래 3요소를 갖고 있어 “Legal Help” POC로 확장하기 유리합니다.

- **Teams Bot 수신/Proactive 전송**: `app/teams/bot.py`
- **멀티테넌트 설정 + 암호화 저장(Supabase)**: `app/core/tenant.py`, `app/admin/routes.py`, `app/utils/crypto.py`
- **대화(=케이스) 매핑 저장**: `app/core/store.py`, `supabase/migrations/001_initial_schema.sql`

즉, 플랫폼 어댑터만 Freshdesk로 추가하면 POC End-to-End를 구성할 수 있습니다.

---

## 3) POC 범위 제안 (12/22 미팅용 “3장면”)

### Scene 1: Teams에서 검토요청 생성(가중치 포함)

- Teams Bot → Adaptive Card 입력:
  - 제목, 내용, 열람자(CC 이메일), 첨부 링크(SharePoint/OneDrive URL)
- Bridge → Freshdesk Ticket 생성:
  - 가중치는 **법무팀이 Freshdesk에서 커스텀 필드로 부여** (요청자 입력 X)
- Teams Bot → “접수번호/상태/링크” 회신

### Scene 2: Freshdesk에서 상태 변경/댓글 → Teams로 알림

- Freshdesk Automation(Webhook) → Bridge `/api/webhook/freshdesk/{tenant}` 호출
- Bridge → Teams Proactive 메시지:
  - “추가정보 요청/상태 변경/담당자 코멘트”를 Teams에 전송

### Scene 3: 상위자 관점 미니 대시보드

- Teams Tab(웹)에서
  - 실원별 진행중/완료 건수
  - 가중치 합(진행중/완료)
  만 보여주는 “아주 작은 테이블”로 충분

---

## 4) 데이터 모델(POC 최소)

### Freshdesk Ticket 필드 매핑(권장)

- Ticket.subject: 검토요청 제목
- Ticket.description: 내용 + 첨부링크
- Ticket.cc_emails: 열람자(요청자 외 CC)
- Ticket.custom_fields:
  - `weight_field_key`: 1~5 (예: `cf_weight`)

### Bridge DB(Supabase)

- `conversations` 테이블:
  - Teams 대화 ID ↔ Freshdesk ticket_id 매핑
  - Proactive 메시지에 필요한 `conversation_reference` 저장

---

## 5) 보안/운영 고려사항 (POC에서 “최소로” 챙길 것)

- Freshdesk API Key는 테넌트 설정에 저장되므로
  - `ENCRYPTION_KEY` 환경변수 설정은 필수(프로덕션 기준)
  - 설정 페이지/로그에 키가 노출되지 않도록 주의
- Webhook 엔드포인트는
  - 테넌트 경로(`/api/webhook/freshdesk/{teams_tenant_id}`)로 라우팅
  - POC에서는 서명검증을 선택으로 두되, 운영 전에는 검증/허용 IP 제한을 권장

---

## 6) 구현 체크리스트 (백엔드)

1. Freshdesk 플랫폼 추가
   - Tenant Platform enum 확장 + 설정 저장/조회 + Admin UI/API 확장
2. Freshdesk API Client 구현
   - Ticket 생성, Note/Reply 추가, Agent 조회(선택)
3. Freshdesk Webhook 파서/라우터 구현
   - ticket_id → 매핑 조회 → Teams Proactive 전송
4. (선택) Teams Adaptive Card 인테이크
   - “검토요청” 트리거 → 카드 발송 → Submit 처리 → Ticket 생성
5. (선택) 대시보드 API + Tab 페이지

---

## 7) 확인이 필요한 운영 파라미터(고객/POC 착수 시)

- Freshdesk(Omni) 사용 여부 및 테넌트/도메인
- 가중치 커스텀 필드 키 (예: `cf_weight`) 확정
- “열람자(CC)”가 실제로 어떤 권한을 의미하는지
  - 단순 참조(메일 CC)인지, Teams에서의 열람 권한까지 포함인지
- Outlook 유입 처리 방식
  - POC: 전달 기반(A) / 본사업: Add-in(B) 여부
