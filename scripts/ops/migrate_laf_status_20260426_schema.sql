-- LAF 狀態流轉 2026-04-26：新增副狀態欄位
-- 執行前請備份：mysqldump law_firm_data cases > /tmp/cases_backup_20260426.sql
-- 執行方式：mysql -u <user> -p law_firm_data < this_file.sql

ALTER TABLE `cases`
  ADD COLUMN IF NOT EXISTS `legal_aid_approval_status`
      VARCHAR(20) NULL DEFAULT NULL
      COMMENT '法扶審核子狀態：暫存/待轉入/已轉入'
      AFTER `legal_aid_status`,
  ADD COLUMN IF NOT EXISTS `legal_aid_approval_checked_at`
      DATETIME NULL DEFAULT NULL
      COMMENT 'verify_portal_closing_status 上次更新時間'
      AFTER `legal_aid_approval_status`;

-- 驗證欄位已建立：
-- SHOW COLUMNS FROM `cases` LIKE 'legal_aid_approval%';
