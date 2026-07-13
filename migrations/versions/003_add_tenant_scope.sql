-- UP

CREATE TABLE IF NOT EXISTS tenants (
    id VARCHAR(64) NOT NULL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    public_base_url VARCHAR(512) DEFAULT '',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_tenants_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS tenant_memberships (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    role VARCHAR(32) NOT NULL DEFAULT 'member',
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_tenant_user (tenant_id, user_id),
    KEY idx_tenant_memberships_user (user_id),
    CONSTRAINT fk_tenant_memberships_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS tenant_api_keys (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    key_hash CHAR(64) NOT NULL,
    label VARCHAR(100) NOT NULL DEFAULT '',
    role VARCHAR(32) NOT NULL DEFAULT 'api',
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at DATETIME NULL,
    UNIQUE KEY uq_tenant_api_key_hash (key_hash),
    KEY idx_tenant_api_keys_tenant (tenant_id),
    CONSTRAINT fk_tenant_api_keys_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS tenant_storage_roots (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    root_path TEXT NOT NULL,
    label VARCHAR(100) NOT NULL DEFAULT '',
    active TINYINT(1) NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_tenant_storage_roots_tenant (tenant_id),
    CONSTRAINT fk_tenant_storage_roots_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT INTO tenants (id, name, status)
VALUES ('default', 'Default Tenant', 'active')
ON DUPLICATE KEY UPDATE name = VALUES(name), status = VALUES(status);

ALTER TABLE IF EXISTS users ADD COLUMN IF NOT EXISTS default_tenant_id VARCHAR(64) NOT NULL DEFAULT 'default';
ALTER TABLE IF EXISTS users ADD INDEX IF NOT EXISTS idx_users_default_tenant (default_tenant_id);

ALTER TABLE IF EXISTS cases ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'default';
ALTER TABLE IF EXISTS cases ADD INDEX IF NOT EXISTS idx_cases_tenant_case_number (tenant_id, case_number);

ALTER TABLE IF EXISTS clients ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'default';
ALTER TABLE IF EXISTS clients ADD INDEX IF NOT EXISTS idx_clients_tenant_name (tenant_id, client_name);

ALTER TABLE IF EXISTS opponents ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'default';
ALTER TABLE IF EXISTS opponents ADD INDEX IF NOT EXISTS idx_opponents_tenant_name (tenant_id, opponent_name);

ALTER TABLE IF EXISTS case_todos ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'default';
ALTER TABLE IF EXISTS case_todos ADD INDEX IF NOT EXISTS idx_case_todos_tenant_case (tenant_id, case_number);

ALTER TABLE IF EXISTS calendar_events ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'default';
ALTER TABLE IF EXISTS calendar_events ADD INDEX IF NOT EXISTS idx_calendar_events_tenant_case (tenant_id, case_number);

ALTER TABLE IF EXISTS document_index ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'default';
ALTER TABLE IF EXISTS document_index ADD INDEX IF NOT EXISTS idx_document_index_tenant_case (tenant_id, case_number);

ALTER TABLE IF EXISTS legal_aid_checklists ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'default';
ALTER TABLE IF EXISTS legal_aid_checklists ADD INDEX IF NOT EXISTS idx_laf_checklists_tenant_case (tenant_id, case_number);

ALTER TABLE IF EXISTS legal_insights ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'default';
ALTER TABLE IF EXISTS legal_insights ADD INDEX IF NOT EXISTS idx_legal_insights_tenant_case (tenant_id, case_number);

ALTER TABLE IF EXISTS court_judgments ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'default';
ALTER TABLE IF EXISTS court_judgments ADD INDEX IF NOT EXISTS idx_court_judgments_tenant_case (tenant_id, case_number);

ALTER TABLE IF EXISTS case_transactions ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'default';
ALTER TABLE IF EXISTS case_transactions ADD INDEX IF NOT EXISTS idx_case_transactions_tenant_case (tenant_id, case_number);

ALTER TABLE IF EXISTS quotations ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'default';
ALTER TABLE IF EXISTS quotations ADD INDEX IF NOT EXISTS idx_quotations_tenant_case (tenant_id, case_number);

ALTER TABLE IF EXISTS activity_logs ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'default';
ALTER TABLE IF EXISTS activity_logs ADD INDEX IF NOT EXISTS idx_activity_logs_tenant_created (tenant_id, created_at);

INSERT INTO tenant_memberships (tenant_id, user_id, role)
SELECT 'default', CAST(id AS CHAR), COALESCE(NULLIF(role, ''), 'member')
FROM users
ON DUPLICATE KEY UPDATE role = VALUES(role), status = 'active';

-- DOWN

ALTER TABLE IF EXISTS activity_logs DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE IF EXISTS quotations DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE IF EXISTS case_transactions DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE IF EXISTS court_judgments DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE IF EXISTS legal_insights DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE IF EXISTS legal_aid_checklists DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE IF EXISTS document_index DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE IF EXISTS calendar_events DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE IF EXISTS case_todos DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE IF EXISTS opponents DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE IF EXISTS clients DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE IF EXISTS cases DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE IF EXISTS users DROP COLUMN IF EXISTS default_tenant_id;
DROP TABLE IF EXISTS tenant_storage_roots;
DROP TABLE IF EXISTS tenant_api_keys;
DROP TABLE IF EXISTS tenant_memberships;
DROP TABLE IF EXISTS tenants;
