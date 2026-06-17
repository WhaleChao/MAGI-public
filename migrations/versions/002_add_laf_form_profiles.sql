-- UP

CREATE TABLE IF NOT EXISTS laf_branch_profiles (
    id INT AUTO_INCREMENT PRIMARY KEY,
    branch_label VARCHAR(100) NOT NULL,
    aliases_json JSON NULL,
    phone VARCHAR(50) DEFAULT '',
    default_lawyer_name VARCHAR(100) NOT NULL DEFAULT '喬政翔律師',
    poa_footer_template TEXT NULL,
    source VARCHAR(100) DEFAULT '',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_laf_branch_label (branch_label)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS laf_law_firm_profiles (
    id INT AUTO_INCREMENT PRIMARY KEY,
    profile_key VARCHAR(50) NOT NULL,
    lawyer_name VARCHAR(100) NOT NULL DEFAULT '喬政翔律師',
    office_name VARCHAR(100) DEFAULT '',
    address_line VARCHAR(255) DEFAULT '',
    phone VARCHAR(50) DEFAULT '',
    fax VARCHAR(50) DEFAULT '',
    mobile VARCHAR(50) DEFAULT '',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_laf_law_firm_profile_key (profile_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT INTO laf_law_firm_profiles
    (profile_key, lawyer_name, office_name, address_line, phone, fax, mobile)
VALUES
    ('default', '喬政翔律師', '喬政翔律師事務所', '970花蓮縣花蓮市明禮路18之6號1樓', '03-835-7186', '03-835-7135', '0937-753-800')
ON DUPLICATE KEY UPDATE
    lawyer_name = VALUES(lawyer_name),
    office_name = VALUES(office_name),
    address_line = VALUES(address_line),
    phone = VALUES(phone),
    fax = VALUES(fax),
    mobile = VALUES(mobile);

INSERT INTO laf_branch_profiles
    (branch_label, aliases_json, phone, default_lawyer_name, poa_footer_template, source)
VALUES
    ('花蓮分會', '["花蓮", "法扶花蓮分會"]', '03-8222128', '喬政翔律師', '本事件經本會　{branch_label}審核准予扶助，爰制作本委任狀如上，自105年10月1日起，本會不再蓋委任狀章。如欲反應律師辦理狀況，請逕致電分會({phone})。', 'local_poa_pdf_scan_20260607')
ON DUPLICATE KEY UPDATE
    aliases_json = VALUES(aliases_json),
    phone = VALUES(phone),
    default_lawyer_name = VALUES(default_lawyer_name),
    poa_footer_template = VALUES(poa_footer_template),
    source = VALUES(source);

INSERT INTO laf_branch_profiles
    (branch_label, aliases_json, phone, default_lawyer_name, poa_footer_template, source)
VALUES
    ('新北分會', '["新北", "法扶新北分會"]', '02-29737778', '喬政翔律師', '本事件經本會　{branch_label}審核准予扶助，爰制作本委任狀如上，自105年10月1日起，本會不再蓋委任狀章。如欲反應律師辦理狀況，請逕致電分會({phone})。', 'local_poa_pdf_scan_20260607')
ON DUPLICATE KEY UPDATE
    aliases_json = VALUES(aliases_json),
    phone = VALUES(phone),
    default_lawyer_name = VALUES(default_lawyer_name),
    poa_footer_template = VALUES(poa_footer_template),
    source = VALUES(source);

INSERT INTO laf_branch_profiles
    (branch_label, aliases_json, phone, default_lawyer_name, poa_footer_template, source)
VALUES
    ('宜蘭分會', '["宜蘭", "法扶宜蘭分會"]', '03-9653531', '喬政翔律師', '本事件經本會　{branch_label}審核准予扶助，爰制作本委任狀如上，自105年10月1日起，本會不再蓋委任狀章。如欲反應律師辦理狀況，請逕致電分會({phone})。', 'local_poa_pdf_scan_20260607')
ON DUPLICATE KEY UPDATE
    aliases_json = VALUES(aliases_json),
    phone = VALUES(phone),
    default_lawyer_name = VALUES(default_lawyer_name),
    poa_footer_template = VALUES(poa_footer_template),
    source = VALUES(source);

INSERT INTO laf_branch_profiles
    (branch_label, aliases_json, phone, default_lawyer_name, poa_footer_template, source)
VALUES
    ('台東分會', '["台東", "臺東", "法扶台東分會", "臺東分會"]', '089-361363', '喬政翔律師', '本事件經本會　{branch_label}審核准予扶助，爰制作本委任狀如上，自105年10月1日起，本會不再蓋委任狀章。如欲反應律師辦理狀況，請逕致電分會({phone})。', 'local_poa_pdf_scan_20260607')
ON DUPLICATE KEY UPDATE
    aliases_json = VALUES(aliases_json),
    phone = VALUES(phone),
    default_lawyer_name = VALUES(default_lawyer_name),
    poa_footer_template = VALUES(poa_footer_template),
    source = VALUES(source);

INSERT INTO laf_branch_profiles
    (branch_label, aliases_json, phone, default_lawyer_name, poa_footer_template, source)
VALUES
    ('台北分會', '["台北", "臺北", "法扶台北分會", "臺北分會"]', '02-23225151', '喬政翔律師', '本事件經本會　{branch_label}審核准予扶助，爰制作本委任狀如上，自105年10月1日起，本會不再蓋委任狀章。如欲反應律師辦理狀況，請逕致電分會({phone})。', 'local_poa_pdf_scan_20260607')
ON DUPLICATE KEY UPDATE
    aliases_json = VALUES(aliases_json),
    phone = VALUES(phone),
    default_lawyer_name = VALUES(default_lawyer_name),
    poa_footer_template = VALUES(poa_footer_template),
    source = VALUES(source);

INSERT INTO laf_branch_profiles
    (branch_label, aliases_json, phone, default_lawyer_name, poa_footer_template, source)
VALUES
    ('基隆分會', '["基隆", "法扶基隆分會"]', '02-24231631', '喬政翔律師', '本事件經本會　{branch_label}審核准予扶助，爰制作本委任狀如上，自105年10月1日起，本會不再蓋委任狀章。如欲反應律師辦理狀況，請逕致電分會({phone})。', 'local_poa_pdf_scan_20260607')
ON DUPLICATE KEY UPDATE
    aliases_json = VALUES(aliases_json),
    phone = VALUES(phone),
    default_lawyer_name = VALUES(default_lawyer_name),
    poa_footer_template = VALUES(poa_footer_template),
    source = VALUES(source);

INSERT INTO laf_branch_profiles
    (branch_label, aliases_json, phone, default_lawyer_name, poa_footer_template, source)
VALUES
    ('士林分會', '["士林", "法扶士林分會"]', '02-28825266', '喬政翔律師', '本事件經本會　{branch_label}審核准予扶助，爰制作本委任狀如上，自105年10月1日起，本會不再蓋委任狀章。如欲反應律師辦理狀況，請逕致電分會({phone})。', 'local_poa_pdf_scan_20260607')
ON DUPLICATE KEY UPDATE
    aliases_json = VALUES(aliases_json),
    phone = VALUES(phone),
    default_lawyer_name = VALUES(default_lawyer_name),
    poa_footer_template = VALUES(poa_footer_template),
    source = VALUES(source);

INSERT INTO laf_branch_profiles
    (branch_label, aliases_json, phone, default_lawyer_name, poa_footer_template, source)
VALUES
    ('原住民族法律服務中心', '["原民", "原民中心", "原住民族法律服務", "原住民族法律服務中心", "法扶原住民族法律服務中心"]', '03-8509917', '喬政翔律師', '本事件經本會　{branch_label}審核准予扶助，爰制作本委任狀如上，自105年10月1日起，本會不再蓋委任狀章。如欲反應律師辦理狀況，請逕致電分會({phone})。', 'local_poa_pdf_scan_20260607')
ON DUPLICATE KEY UPDATE
    aliases_json = VALUES(aliases_json),
    phone = VALUES(phone),
    default_lawyer_name = VALUES(default_lawyer_name),
    poa_footer_template = VALUES(poa_footer_template),
    source = VALUES(source);


-- DOWN

DROP TABLE IF EXISTS laf_branch_profiles;

DROP TABLE IF EXISTS laf_law_firm_profiles;
