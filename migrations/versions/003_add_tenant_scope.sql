-- MAGI tenant-scope schema, version 003.
--
-- This migration is deliberately idempotent for MariaDB 10.11.  The runtime
-- helper api.saas_schema.apply_tenant_schema performs the same operations with
-- the configured tenant identity; this file is the version-controlled rebuild
-- contract for new databases and disaster recovery.

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
    KEY idx_tenant_memberships_tenant (tenant_id)
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
    KEY idx_tenant_api_keys_tenant (tenant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS tenant_storage_roots (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    root_path TEXT NOT NULL,
    label VARCHAR(100) NOT NULL DEFAULT '',
    active TINYINT(1) NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_tenant_storage_roots_tenant (tenant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

DELIMITER //
DROP PROCEDURE IF EXISTS magi_add_tenant_scope//
CREATE PROCEDURE magi_add_tenant_scope(
    IN p_table VARCHAR(64),
    IN p_column VARCHAR(64)
)
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = DATABASE() AND table_name = p_table
    ) THEN
        SET @magi_sql = CONCAT(
            'ALTER TABLE `', p_table, '` ADD COLUMN IF NOT EXISTS `', p_column,
            '` VARCHAR(64) NOT NULL DEFAULT ''default'''
        );
        PREPARE magi_stmt FROM @magi_sql;
        EXECUTE magi_stmt;
        DEALLOCATE PREPARE magi_stmt;

        SET @magi_sql = CONCAT(
            'UPDATE `', p_table, '` SET `', p_column,
            '` = ''default'' WHERE `', p_column,
            '` IS NULL OR `', p_column, '` = '''''
        );
        PREPARE magi_stmt FROM @magi_sql;
        EXECUTE magi_stmt;
        DEALLOCATE PREPARE magi_stmt;

        SET @magi_index = LEFT(CONCAT('idx_', p_table, '_', p_column), 60);
        IF NOT EXISTS (
            SELECT 1
            FROM information_schema.statistics
            WHERE table_schema = DATABASE()
              AND table_name = p_table
              AND index_name = @magi_index
        ) THEN
            SET @magi_sql = CONCAT(
                'CREATE INDEX `', @magi_index, '` ON `', p_table, '` (`', p_column, '`)'
            );
            PREPARE magi_stmt FROM @magi_sql;
            EXECUTE magi_stmt;
            DEALLOCATE PREPARE magi_stmt;
        END IF;
    END IF;
END//
DELIMITER ;

CALL magi_add_tenant_scope('users', 'default_tenant_id');
CALL magi_add_tenant_scope('documents', 'tenant_id');
CALL magi_add_tenant_scope('vectors', 'tenant_id');
CALL magi_add_tenant_scope('messages', 'tenant_id');
CALL magi_add_tenant_scope('tasks', 'tenant_id');
CALL magi_add_tenant_scope('audit_log', 'tenant_id');
CALL magi_add_tenant_scope('pending_registrations', 'tenant_id');
CALL magi_add_tenant_scope('cases', 'tenant_id');
CALL magi_add_tenant_scope('clients', 'tenant_id');
CALL magi_add_tenant_scope('opponents', 'tenant_id');
CALL magi_add_tenant_scope('case_todos', 'tenant_id');
CALL magi_add_tenant_scope('calendar_events', 'tenant_id');
CALL magi_add_tenant_scope('case_calendar_events', 'tenant_id');
CALL magi_add_tenant_scope('case_checklists', 'tenant_id');
CALL magi_add_tenant_scope('case_documents', 'tenant_id');
CALL magi_add_tenant_scope('document_index', 'tenant_id');
CALL magi_add_tenant_scope('legal_aid_checklists', 'tenant_id');
CALL magi_add_tenant_scope('legal_insights', 'tenant_id');
CALL magi_add_tenant_scope('court_judgments', 'tenant_id');
CALL magi_add_tenant_scope('case_transactions', 'tenant_id');
CALL magi_add_tenant_scope('quotations', 'tenant_id');
CALL magi_add_tenant_scope('activity_logs', 'tenant_id');
CALL magi_add_tenant_scope('settings', 'tenant_id');
CALL magi_add_tenant_scope('user_settings', 'tenant_id');
CALL magi_add_tenant_scope('learning_history', 'tenant_id');

DROP PROCEDURE IF EXISTS magi_add_tenant_scope;
