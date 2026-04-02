# MAGI Data Retention & Classification Policy

**Effective Date:** March 19, 2026
**Last Updated:** March 19, 2026
**Version:** 1.0

---

## 1. Executive Summary

This policy establishes data classification tiers, retention periods, deletion procedures, and backup retention schedules for MAGI's Multi-Agent Governance Infrastructure. It applies to all data stored, processed, or archived by MAGI systems.

**Key Principles:**
- Minimize data retention: Keep only what's necessary
- Classify data by sensitivity: Apply appropriate controls
- Automate deletion: Use scheduled purges where possible
- Document everything: Maintain audit trail of deletions

---

## 2. Data Classification Framework

### 2.1 Classification Tiers

MAGI uses a 4-tier data classification system:

#### Tier 1: PUBLIC
- **Definition**: Data that can be disclosed without harm
- **Examples**: Marketing materials, public documentation, aggregated statistics
- **Access**: Unrestricted
- **Retention**: As needed, minimum restrictions
- **Encryption**: Not required
- **Backup Frequency**: Weekly

#### Tier 2: INTERNAL
- **Definition**: Confidential business data for internal use only
- **Examples**: User account metadata, API logs, system metrics, employee data
- **Access**: Employees and authorized contractors only
- **Retention**: 1-3 years depending on type
- **Encryption**: Recommended for sensitive internal data
- **Backup Frequency**: Daily

#### Tier 3: CONFIDENTIAL
- **Definition**: Sensitive personal or business data; unauthorized disclosure would cause harm
- **Examples**: User passwords, API keys, financial records, health data, location data
- **Access**: Need-to-know basis; role-based access control
- **Retention**: As short as possible (typically 6-12 months)
- **Encryption**: Required (AES-256 or stronger)
- **Backup Frequency**: Daily
- **Special Handling**: Encryption keys stored separately

#### Tier 4: RESTRICTED
- **Definition**: Highly sensitive data; disclosure prohibited by law
- **Examples**: Payment card data (PCI), social security numbers, judicial records with restricted access
- **Access**: Extremely limited; logged and audited
- **Retention**: Minimum required by law only
- **Encryption**: Required; encryption keys in HSM or external vault
- **Backup Frequency**: Encrypted daily backups, separately secured
- **Audit**: Full audit trail of all access

---

## 3. Data Type Retention Periods

### 3.1 User & Account Data

| Data Type | Classification | Retention | Trigger |
|-----------|---|---|---|
| User Profile | INTERNAL | Until account deletion | Account termination |
| Authentication Logs | INTERNAL | 90 days | Log rotation policy |
| Session Logs | INTERNAL | 30 days | Automatic purge |
| Passwords (hashed) | CONFIDENTIAL | Until account deletion | Account termination |
| API Keys | CONFIDENTIAL | 6 months after rotation | Key rotation schedule |
| MFA/2FA Codes | CONFIDENTIAL | 24 hours | Code expiration |

### 3.2 Judicial & Case Data

| Data Type | Classification | Retention | Trigger |
|-----------|---|---|---|
| Case Files | INTERNAL | Duration + 7 years | Legal requirement |
| Judgment Summaries | INTERNAL | Duration + 7 years | Legal requirement |
| Translations | INTERNAL | Duration + 5 years | Archive after use |
| User Annotations | INTERNAL | Duration + 3 years | Case closure |
| Case Metadata | INTERNAL | Duration + 7 years | Legal requirement |

**Note**: "Duration" = time case is active in system; 7-year retention is mandated by Taiwan judicial records law.

### 3.3 Communication & Interaction Data

| Data Type | Classification | Retention | Trigger |
|-----------|---|---|---|
| Chat Messages | INTERNAL | 1 year | Automatic archival |
| Email Communications | INTERNAL | 3 years | Regulatory requirement |
| Discord Integration Logs | INTERNAL | 90 days | Platform retention |
| LINE Integration Logs | INTERNAL | 90 days | Platform retention |
| Telegram Integration Logs | INTERNAL | 90 days | Platform retention |
| Support Tickets | INTERNAL | 2 years | Issue closure + 24 months |

### 3.4 System & Technical Data

| Data Type | Classification | Retention | Trigger |
|-----------|---|---|---|
| API Call Logs | INTERNAL | 180 days | Automatic purge |
| Error Logs | INTERNAL | 90 days | Automatic purge |
| Audit Logs | CONFIDENTIAL | 7 years | Legal/compliance |
| Database Query Logs | INTERNAL | 30 days | Automatic purge |
| Performance Metrics | INTERNAL | 1 year | Data aggregation |
| Access Logs | CONFIDENTIAL | 90 days | Security review |

### 3.5 Backup & Recovery Data

| Data Type | Classification | Retention | Trigger |
|-----------|---|---|---|
| Daily Backups | CONFIDENTIAL | 30 days | Automated purge |
| Weekly Backups | CONFIDENTIAL | 90 days | Automated purge |
| Monthly Backups | CONFIDENTIAL | 1 year | Automated purge |
| Quarterly Backups | CONFIDENTIAL | 3 years | Disaster recovery |
| Annual Backups | CONFIDENTIAL | 7 years | Compliance archival |
| Point-in-Time Backups | CONFIDENTIAL | As needed, max 7 days | Manual deletion |

---

## 4. Deletion Procedures

### 4.1 Automated Deletion

The following data is deleted automatically via scheduled jobs:

```bash
# Daily at 02:00 UTC
0 2 * * * /scripts/purge_session_logs.sh
0 2 * * * /scripts/purge_api_logs.sh
0 2 * * * /scripts/purge_error_logs.sh

# Weekly (Sunday at 03:00 UTC)
0 3 * * 0 /scripts/purge_temp_data.sh
0 3 * * 0 /scripts/purge_expired_backups.sh

# Monthly (1st at 04:00 UTC)
0 4 1 * * /scripts/archive_case_data.sh
```

### 4.2 Manual Deletion Process

For data not covered by automated purge:

#### Step 1: Identification
- Identify data for deletion (account termination, user request, etc.)
- Document the reason and authorization
- Record deletion in audit log

#### Step 2: Verification
- Verify user identity (for user-initiated deletions)
- Confirm legal basis for deletion
- Check for dependent data or cross-references

#### Step 3: Backup Consideration
- Determine if data should be removed from active backups
- Schedule removal from backup retention cycle
- Note in backup manifest

#### Step 4: Execution
```bash
# Example deletion procedure:
bin/data-delete --user-id=<id> --reason="account_termination" --verify
```

#### Step 5: Verification & Audit
- Verify data was deleted from all systems (primary, cache, backups)
- Log deletion action with timestamp, operator, and reason
- Generate deletion certificate if requested

#### Step 6: Documentation
- Update data inventory
- Notify user if applicable
- Archive deletion request for 7 years

### 4.3 Right to Be Forgotten (GDPR Article 17)

For explicit deletion requests:

1. **User Submits Request** → Submit via privacy request form
2. **Verification** (7 days) → Verify identity and eligibility
3. **Assessment** (14 days) → Determine what can be deleted
4. **Deletion** (30 days) → Remove from all systems
5. **Confirmation** (35 days) → Notify user of completion

**Exceptions** (data may be retained):
- Legal obligation (court order, regulatory requirement)
- Exercise of free speech or information rights
- Public interest or research (anonymized)
- Legal claim or evidence
- Data doesn't relate to the user

---

## 5. Backup Retention Schedule

### 5.1 Standard Backup Tiers

```
Daily Backups (30 days):
  Mon 03/17 → Purged on 04/16
  Tue 03/18 → Purged on 04/17
  Wed 03/19 → Purged on 04/18
  ... (rotates every 30 days)

Weekly Backups (90 days):
  Week 1 (03/17) → Purged on 06/15
  Week 2 (03/24) → Purged on 06/22
  Week 3 (03/31) → Purged on 07/29
  ... (rotates every 90 days)

Monthly Backups (1 year):
  Jan 2026 → Purged Jan 2027
  Feb 2026 → Purged Feb 2027
  Mar 2026 → Purged Mar 2027
  ... (rotates annually)

Annual/Compliance Backups (7 years):
  2026 → Purged 2033
  2027 → Purged 2034
  2028 → Purged 2035
  ... (purged after 7 years)
```

### 5.2 Backup Encryption & Storage

- All backups encrypted with AES-256
- Encryption keys stored separately (Hardware Security Module)
- Off-site replication for disaster recovery
- Backup integrity verified monthly (SHA-256 checksums)
- Access to backups requires MFA and audit approval

### 5.3 Backup Deletion Procedures

```bash
# Automated daily purge (runs at 01:00 UTC)
/scripts/purge_old_backups.sh \
  --retention-daily=30 \
  --retention-weekly=90 \
  --retention-monthly=365 \
  --retention-annual=2555 \
  --verify-before-delete
```

### 5.4 Emergency Retention Exceptions

Backups may be retained beyond normal schedule if:
- **Legal Hold**: Court order or ongoing litigation (indefinite)
- **Investigation**: Active security or compliance investigation (60 days min)
- **Disaster Recovery**: Recent backup needed for failover (extended to 7 days)
- **Regulatory Audit**: Requested by auditor or regulator (until resolved)

---

## 6. Data Retention Matrix (Quick Reference)

| Category | Data Type | Classification | Retention | Auto-Purge |
|---|---|---|---|---|
| **Users** | Profile | INTERNAL | Until deletion | 90 days after |
| | Passwords | CONFIDENTIAL | Until deletion | ✓ |
| | Session Logs | INTERNAL | 30 days | ✓ |
| **Cases** | Files | INTERNAL | 7 years + | Manual |
| | Metadata | INTERNAL | 7 years + | Manual |
| | Annotations | INTERNAL | 3 years | Manual |
| **System** | API Logs | INTERNAL | 180 days | ✓ |
| | Error Logs | INTERNAL | 90 days | ✓ |
| | Audit Logs | CONFIDENTIAL | 7 years | No |
| **Backups** | Daily | CONFIDENTIAL | 30 days | ✓ |
| | Weekly | CONFIDENTIAL | 90 days | ✓ |
| | Monthly | CONFIDENTIAL | 1 year | ✓ |
| | Annual | CONFIDENTIAL | 7 years | ✓ |

---

## 7. Data Inventory & Mapping

### 7.1 Maintaining the Inventory

All data stored by MAGI must be registered in the Data Inventory:

```yaml
data_type: "case_files"
classification: "INTERNAL"
location: "MySQL / /mnt/cases"
owner: "Legal Team"
processor: "database_admin"
purpose: "Judicial case tracking"
retention_period: "7 years + active"
encryption: true
backup_frequency: "daily"
last_reviewed: "2026-03-19"
next_review: "2026-09-19"
```

### 7.2 Annual Review

Every data asset must be reviewed annually:
- Confirm classification level
- Verify retention period is still appropriate
- Check if data is still necessary
- Update access controls
- Document review outcome

---

## 8. Special Cases & Exceptions

### 8.1 User-Initiated Deletion

When a user requests deletion of their data:

1. **Immediate Deletion**
   - Delete from live database
   - Remove from caches
   - Delete from current backups if possible

2. **Backup Cleanup**
   - Flag in backup manifest as "user-deleted"
   - Restore-point exclusion list updated
   - Automated expiration after backup retention expires

3. **Verification**
   - Confirm deletion with database query
   - Document in audit log
   - Send confirmation to user

### 8.2 Account Termination

When a user account is terminated:

1. **Immediate Actions**
   - Disable API keys and sessions
   - Archive user data to separate storage
   - Flag all user data for retention review

2. **Retention Period**
   - Keep for 90 days (backup and investigation window)
   - After 90 days, delete per normal retention schedule

3. **Exception**
   - If account holds active cases: retain for case duration + retention period

### 8.3 Data Breaches

In case of a security breach:

1. **Affected Data**: Determine scope of exposure
2. **Accelerated Deletion**: Delete exposed data immediately if not needed
3. **Notification**: Notify affected users within 72 hours
4. **Retention Hold**: Keep breach investigation data for 1 year
5. **Evidence**: Preserve forensic evidence per legal hold procedures

### 8.4 Legal Hold

When litigation or investigation is pending:

1. **Designation**: Legal team issues data preservation order
2. **Hold Flag**: Mark data with legal hold flag
3. **No Deletion**: Override automatic purge procedures
4. **Monitoring**: Regular audit of held data
5. **Release**: Delete when legal matter resolved

---

## 9. Compliance & Regulatory Requirements

### 9.1 Taiwan PDPA (Personal Data Protection Act)

- Retention: Only retain data necessary for stated purpose
- Deletion: Delete or anonymize within reasonable period
- Audit: Maintain audit trail of deletions
- Verification: Annual compliance review

### 9.2 Judicial Records (Taiwan Law)

- Case files: Retain for 7 years after judgment
- Transcript: Retain for 10 years per court guidelines
- Metadata: Retain for 7 years per judicial records law

### 9.3 GDPR (If EU users)

- Retention: Only as long as necessary
- Deletion: Right to be Forgotten within 30 days
- Cross-Border: Maintain SCC agreements for transfers
- Audit: Annual data protection impact assessment (DPIA)

### 9.4 PCI DSS (If payment data stored)

- Retention: Keep only 12 months
- Encryption: PCI-compliant AES-256 encryption
- Access: Minimal access, fully audited
- Deletion: Secure shredding/cryptographic erasure

---

## 10. Implementation & Automation

### 10.1 Automated Purge Jobs

```bash
# File: cron_purge.sh
# Runs via: crontab -e

# Session logs (daily, 30-day window)
0 2 * * * /scripts/purge_session_logs.sh --days=30

# API logs (daily, 180-day window)
0 2 * * * /scripts/purge_api_logs.sh --days=180

# Error logs (daily, 90-day window)
0 2 * * * /scripts/purge_error_logs.sh --days=90

# Expired backups (weekly)
0 3 * * 0 /scripts/purge_old_backups.sh

# Archived case data (monthly)
0 4 1 * * /scripts/archive_old_cases.sh --days=365
```

### 10.2 Monitoring & Verification

Check purge job status:
```bash
# View last run
tail -f /var/log/magi/purge.log

# Verify data was actually deleted
SELECT COUNT(*) FROM sessions WHERE created_at < DATE_SUB(NOW(), INTERVAL 30 DAY);

# Generate retention report
python scripts/data_retention_report.py --month=$(date +%Y-%m)
```

### 10.3 Audit & Compliance Reporting

Monthly compliance report:
```bash
/scripts/generate_retention_report.py \
  --format=pdf \
  --include-exceptions \
  --sign-with-dpo \
  --output=reports/data_retention_$(date +%Y-%m).pdf
```

---

## 11. Data Retention Violations & Remediation

### 11.1 Identifying Violations

Automated checks run daily:

```bash
# Find data beyond retention period
SELECT * FROM sessions WHERE created_at < DATE_SUB(NOW(), INTERVAL 30 DAY);

# Find unclassified data
SELECT * FROM data_inventory WHERE classification IS NULL;

# Find data without backup schedule
SELECT * FROM data_inventory WHERE backup_frequency IS NULL;
```

### 11.2 Remediation Process

1. **Discovery** → Automated alert or manual audit identifies violation
2. **Assessment** → Determine scope, cause, and impact
3. **Notification** → Inform DPO and compliance team within 24 hours
4. **Remediation** → Delete data or establish legal basis for retention
5. **Verification** → Confirm deletion, document in audit trail
6. **Reporting** → File incident report and corrective action

---

## 12. Roles & Responsibilities

### 12.1 Data Owners
- Classify data by tier
- Determine retention requirements
- Approve deletion requests
- Respond to user data access requests

### 12.2 Data Custodians
- Implement deletion procedures
- Monitor automated purge jobs
- Maintain backup retention schedule
- Respond to recovery requests

### 12.3 DPO / Privacy Officer
- Oversee data retention policy
- Review legal holds
- Approve exceptions and variations
- Conduct annual compliance review

### 12.4 Compliance & Legal
- Monitor regulatory changes
- Issue legal holds
- Review deletion exceptions
- Advise on data retention conflicts

### 12.5 Security Team
- Encrypt sensitive backups
- Manage encryption keys
- Audit access to retained data
- Investigate data breach retention

---

## 13. Policy Updates & Review

### 13.1 Annual Review Schedule

- **Q1 (Jan-Mar)**: Legal and regulatory review
- **Q2 (Apr-Jun)**: Audit findings and compliance gaps
- **Q3 (Jul-Sep)**: Technology and process improvements
- **Q4 (Oct-Dec)**: Final review and next-year planning

### 13.2 Change Management

When data retention policy changes:
1. **Announcement** → Email notification to all departments
2. **Transition Period** → 30 days to comply with new periods
3. **Implementation** → Update retention rules and automated jobs
4. **Verification** → Audit that old data is purged per new schedule
5. **Documentation** → Update this policy document

---

## 14. Appendices

### Appendix A: Glossary

- **Data Classification**: Categorization of data by sensitivity
- **Retention Period**: Duration data must be kept
- **Purge**: Permanent deletion of data
- **Archive**: Long-term storage with restricted access
- **Backup**: Copy of data for disaster recovery
- **Legal Hold**: Preservation order preventing deletion
- **Right to Be Forgotten**: User's right to data deletion

### Appendix B: Related Policies

- [MAGI Privacy Policy](./PRIVACY_POLICY.md)
- [MAGI Security Policy](./SECURITY_POLICY.md)
- [MAGI Backup & Recovery Policy](./BACKUP_RECOVERY_POLICY.md)
- [MAGI Data Protection Impact Assessment (DPIA)](./DPIA.md)

### Appendix C: Contact

**Data Protection Officer (DPO):**
- Email: [INSERT PRIVACY_EMAIL]
- Phone: [INSERT CONTACT_PHONE]
- Address: [INSERT OFFICE_ADDRESS]

---

**Policy Version:** 1.0 | **Effective Date:** March 19, 2026 | **Next Review:** March 19, 2027
