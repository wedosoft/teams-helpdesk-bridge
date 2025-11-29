-- Teams-Helpdesk Bridge 초기 스키마
-- Supabase SQL Editor에서 실행

-- 테넌트 설정
CREATE TABLE IF NOT EXISTS tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    teams_tenant_id TEXT UNIQUE NOT NULL,
    platform TEXT NOT NULL CHECK (platform IN ('freshchat', 'zendesk', 'salesforce', 'freshdesk')),

    -- 플랫폼 인증 정보 (암호화)
    platform_config JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- 설정
    bot_name TEXT DEFAULT 'IT Helpdesk',
    welcome_message TEXT,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 대화 매핑
CREATE TABLE IF NOT EXISTS conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,

    -- Teams 정보
    teams_conversation_id TEXT NOT NULL,
    teams_user_id TEXT NOT NULL,
    conversation_reference JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- 플랫폼 정보
    platform TEXT NOT NULL,
    platform_conversation_id TEXT NOT NULL,
    platform_user_id TEXT,

    -- 상태
    is_resolved BOOLEAN DEFAULT FALSE,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(teams_conversation_id, platform)
);

-- 사용자 프로필 캐시
CREATE TABLE IF NOT EXISTS user_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    teams_user_id TEXT UNIQUE NOT NULL,

    display_name TEXT,
    email TEXT,
    job_title TEXT,
    department TEXT,

    cached_at TIMESTAMPTZ DEFAULT NOW()
);

-- 인덱스
CREATE INDEX IF NOT EXISTS idx_conversations_teams ON conversations(teams_conversation_id);
CREATE INDEX IF NOT EXISTS idx_conversations_platform ON conversations(platform, platform_conversation_id);
CREATE INDEX IF NOT EXISTS idx_tenants_teams ON tenants(teams_tenant_id);
CREATE INDEX IF NOT EXISTS idx_user_profiles_teams ON user_profiles(teams_user_id);

-- updated_at 자동 업데이트 함수
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- updated_at 트리거
DROP TRIGGER IF EXISTS update_tenants_updated_at ON tenants;
CREATE TRIGGER update_tenants_updated_at
    BEFORE UPDATE ON tenants
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_conversations_updated_at ON conversations;
CREATE TRIGGER update_conversations_updated_at
    BEFORE UPDATE ON conversations
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- RLS (Row Level Security) 정책 - 필요시 활성화
-- ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE user_profiles ENABLE ROW LEVEL SECURITY;
