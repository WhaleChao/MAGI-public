-- MAGI Brain Setup Script (Side-car Pattern)
-- ⚠️ 警告: 請由 DBA 或系統管理員執行
-- 目的: 建立獨立的 magi_brain 資料庫，並設定 osc 的唯讀保護

-- 1. 建立 MAGI 協作大腦 (The Shared Brain)
CREATE DATABASE IF NOT EXISTS magi_brain CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE magi_brain;

-- 2. 建立任務表 (Task Board)
CREATE TABLE IF NOT EXISTS tasks (
    id INT AUTO_INCREMENT PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    status ENUM('inbox', 'assigned', 'in_progress', 'review', 'done', 'blocked') DEFAULT 'inbox',
    assignee_id VARCHAR(50), -- e.g., 'MAGI-02' (Melchior)
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- 3. 建立訊息流 (Message Bus)
CREATE TABLE IF NOT EXISTS messages (
    id INT AUTO_INCREMENT PRIMARY KEY,
    task_id INT,
    sender_id VARCHAR(50), -- e.g., 'User', 'MAGI-01'
    content TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

-- 4. 建立審計日誌 (Iron Dome Audit Log)
-- 任何對 osc 的修改嘗試都必須先寫入這裡
CREATE TABLE IF NOT EXISTS audit_log (
    id INT AUTO_INCREMENT PRIMARY KEY,
    agent_name VARCHAR(50) NOT NULL,
    target_db VARCHAR(50) DEFAULT 'law_firm_data',
    table_name VARCHAR(50) NOT NULL,
    record_id INT NOT NULL,
    operation ENUM('INSERT', 'UPDATE') NOT NULL,
    old_value JSON COMMENT '修改前的完整資料快照',
    new_value JSON COMMENT '修改後的預期資料',
    reason TEXT COMMENT 'MAGI 修改的理由',
    executed_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 5. 建立使用者與權限系統 (Federation Identity)
CREATE TABLE IF NOT EXISTS users (
    id VARCHAR(50) PRIMARY KEY, -- e.g. 'Lumi6'
    line_user_id VARCHAR(100) UNIQUE, -- LINE User ID (Uxxxxxxxx...)
    role ENUM('admin', 'guest') DEFAULT 'guest',
    api_key VARCHAR(100), 
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 暫存註冊碼表 (For First Contact Protocol)
CREATE TABLE IF NOT EXISTS pending_registrations (
    token VARCHAR(20) PRIMARY KEY,
    role ENUM('admin', 'guest') DEFAULT 'admin',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Admin Logic: The system will automatically register the Active OS User of Castper as Admin on first run.
-- No hardcoded users here.

-- 6. 建立 MAGI 專用帳號與權限 (Secure Access)
-- 請將 'secure_password' 替換為實際的強密碼
CREATE USER IF NOT EXISTS 'magi_agent'@'%' IDENTIFIED BY 'secure_password';

-- 權限 A: 對 magi_brain 擁有完全控制權
GRANT ALL PRIVILEGES ON magi_brain.* TO 'magi_agent'@'%';

-- 權限 B: 對 law_firm_data 擁有唯讀 + 新增 + 修改 (禁止刪除)
-- 假設您的業務資料庫名稱為 'law_firm_data'
GRANT SELECT, INSERT, UPDATE ON law_firm_data.* TO 'magi_agent'@'%';

-- 關鍵防護: 撤銷 DELETE 與 DROP 權限
REVOKE DELETE, DROP ON law_firm_data.* FROM 'magi_agent'@'%';

-- 重新載入權限
FLUSH PRIVILEGES;

-- 完成
SELECT 'MAGI Brain setup completed. Iron Dome active.' as Status;
