# MAGI Privacy Policy

**Effective Date:** March 19, 2026
**Last Updated:** March 19, 2026
**Version:** 1.0

---

## 1. Introduction

This Privacy Policy describes how MAGI ("we," "us," "our," or the "Company") collects, uses, discloses, and otherwise processes personal information in connection with our Multi-Agent Governance Infrastructure platform (the "Service").

We are committed to protecting your privacy and ensuring transparency about how we handle your data. This policy applies to all users of MAGI, including individuals, organizations, and automated systems.

---

## 2. What Data MAGI Collects

### 2.1 Information You Provide Directly

#### User Account Information
- Name, email address, username
- Authentication credentials (hashed passwords, API keys)
- Organization/company affiliation
- User preferences and settings

#### Content & Communications
- Documents, text, and files you upload or input
- Chat messages and conversation logs
- Administrative notes and annotations
- Case files, legal documents, and analysis requests

#### Judicial Records & Case Data
- Court case numbers, judgment dates, and verdicts
- Judge names, counsel information, and party identifiers
- Judicial record summaries and annotations
- Translation and summarization outputs
- User-created tags and classifications

#### Metadata
- Session timestamps and duration
- IP addresses and device information
- Browser type and version
- Operating system and language preferences
- User agent strings

### 2.2 Information Collected Automatically

#### System & Performance Data
- API call frequency and response times
- Feature usage statistics
- Error logs and exception reports
- Database query patterns and performance metrics
- System load and resource utilization

#### Interaction Data
- Pages/features accessed
- Buttons clicked, forms submitted
- Search queries and filter selections
- Export and download requests
- Export format and frequency

#### Technical Data
- Cookies and session identifiers
- Local storage and cache contents
- Log files containing request/response data
- Network performance metrics

### 2.3 Data From Third-Party Sources

- Authentication data from SSO providers (Google, LDAP, OAuth)
- User information from organization directory integrations
- Judicial records from public court databases
- Judicial API feeds and webhooks

---

## 3. How MAGI Uses Your Data

### 3.1 Primary Use Cases

#### Service Delivery
- Process judicial records and generate summaries
- Translate legal documents between languages
- Authenticate users and manage access control
- Maintain user sessions and preferences
- Backup and disaster recovery

#### Analytics & Improvement
- Analyze feature usage patterns
- Identify performance bottlenecks
- Debug errors and improve stability
- Measure user engagement and satisfaction
- Conduct A/B testing on UI/UX

#### Legal & Compliance
- Verify user identity and prevent fraud
- Enforce Terms of Service
- Comply with legal obligations (subpoenas, court orders)
- Maintain audit trails for regulatory compliance
- Detect and prevent abuse

#### Communications
- Send service updates and important notices
- Notify of security incidents
- Respond to user inquiries and support requests
- Provide usage reports and billing information

### 3.2 Secondary Use Cases

- Develop new features and services
- Conduct research on judicial system trends
- Create anonymized datasets for academic research
- Train machine learning models (with explicit consent)

---

## 4. Data Storage & Processing

### 4.1 Data Storage Location

MAGI stores data in:
- **Primary Database**: MySQL/MariaDB (on-premises or cloud provider)
- **File Storage**: Local file system or cloud storage (S3-compatible)
- **Caches**: Redis (optional, for performance)
- **Backups**: Encrypted backups with 30-90 day retention

### 4.2 Data Encryption

- **In Transit**: All API communication uses HTTPS/TLS 1.2+
- **At Rest**: Sensitive data (passwords, API keys) encrypted with AES-256
- **Backup Encryption**: Encrypted with organization's backup key
- **Database Passwords**: Stored as hashed values using bcrypt/argon2

### 4.3 Data Segregation

- User data isolated by organization/account
- Judicial records separated from metadata
- System logs segregated from user content
- Personal identifiers separated from analytical data (where possible)

### 4.4 Access Controls

- Role-based access control (RBAC)
- Multi-factor authentication (MFA) for administrators
- API key rotation and lifecycle management
- Session timeouts and inactivity lockouts
- Principle of least privilege for system accounts

---

## 5. Data Retention Policy

### 5.1 Active Account Data
- **User Profiles**: Retained while account is active, deleted 90 days after termination
- **Case Data**: Retained for duration of case processing + 7 years (regulatory requirement)
- **Session Logs**: Retained for 90 days, then archived
- **API Logs**: Retained for 180 days, then deleted
- **Audit Logs**: Retained for 7 years (compliance requirement)

### 5.2 Backup Data
- **Daily Backups**: Retained for 30 days
- **Weekly Backups**: Retained for 90 days
- **Monthly Backups**: Retained for 1 year
- **Archived Backups**: Retained for 7 years (compliance)

### 5.3 Deletion Upon Request
- User can request deletion of personal data (Right to Be Forgotten)
- Deletion occurs within 30 days of verified request
- Automated systems purge from backups after 90 days

---

## 6. Third-Party Services & Data Sharing

### 6.1 Service Providers

MAGI uses third-party services that may process user data:

| Service | Purpose | Data Shared | Privacy Link |
|---------|---------|-------------|--------------|
| Google OAuth | Authentication | Email, name, profile | [Google Privacy](https://policies.google.com/privacy) |
| Discord API | Channel integration | User ID, messages | [Discord Privacy](https://discord.com/privacy) |
| LINE Messaging | Channel integration | User ID, messages | [LINE Privacy](https://terms.line.me/line_rules_en) |
| MySQL Cloud | Database hosting | Encrypted data | Provider's Policy |
| S3-Compatible Storage | File storage | Encrypted files | Provider's Policy |
| Email Service | Transactional emails | Email addresses | Provider's Policy |

### 6.2 Data Sharing Restrictions

MAGI **does not** share personal data with third parties except:
- Service providers under Data Processing Agreements (DPA)
- Legal compliance with law enforcement (with warrant/subpoena)
- Aggregated/anonymized datasets for research
- With explicit user consent

### 6.3 Sub-processors

All sub-processors are listed in our Sub-processor Registry. Users can request access via privacy requests.

---

## 7. Data Security & Breach Notification

### 7.1 Security Measures

- Annual security audits and penetration testing
- Vulnerability scanning and patch management
- Intrusion detection and logging
- Rate limiting and DDoS protection
- Web Application Firewall (WAF)
- Database activity monitoring

### 7.2 Breach Response

In case of a data breach:
1. **Immediate**: Contain breach, assess scope
2. **Within 24 Hours**: Notify security team and leadership
3. **Within 72 Hours**: Notify affected users (where required by law)
4. **Within 30 Days**: Provide breach report with details and remediation

Breach notification will include:
- What data was breached
- How many users affected
- What measures we took
- What users should do

---

## 8. User Rights & Choices

### 8.1 Access & Portability
- **Right to Access**: Request a copy of your personal data
- **Right to Portability**: Download your data in machine-readable format (JSON, CSV)
- **Request Process**: Submit to [PRIVACY_EMAIL] with verification

### 8.2 Correction & Deletion
- **Right to Rectification**: Update inaccurate or incomplete data
- **Right to Erasure**: Request deletion (Right to Be Forgotten)
- **Request Process**: Submit to [PRIVACY_EMAIL]

### 8.3 Withdrawal & Objection
- **Withdraw Consent**: Opt-out of optional data collection
- **Object to Processing**: Request limitation of data use
- **Contact**: [PRIVACY_EMAIL]

### 8.4 Exercising Your Rights
- All requests must be in writing and include proof of identity
- We will respond within 30 days
- Some requests may take longer depending on complexity

---

## 9. Data Protection Compliance

### 9.1 Jurisdictions Covered

This policy complies with:
- **Taiwan**: Personal Data Protection Act (PDPA)
- **EU**: General Data Protection Regulation (GDPR)
- **US**: State privacy laws (CCPA, CPRA, etc.)
- **Other**: Relevant data protection regulations in user's jurisdiction

### 9.2 Legal Basis for Processing

We process personal data under these legal bases:
- **Contractual Necessity**: To provide the Service
- **Legitimate Interest**: To improve service and prevent fraud
- **Legal Obligation**: To comply with laws
- **Consent**: For optional data collection
- **Legal Claims**: To establish, exercise, or defend claims

---

## 10. International Data Transfers

If you are located outside our data center jurisdiction:
- We use Standard Contractual Clauses (SCCs) for transfers
- We implement supplementary safeguards for data protection
- You have the right to request transfer details

---

## 11. Children's Privacy

MAGI is not intended for users under 18 years old. We do not knowingly collect data from children. If we become aware of data from a minor, we will delete it promptly.

---

## 12. Policy Updates

We may update this policy to reflect changes in:
- Regulations and compliance requirements
- Technology and security practices
- Service features and functionality

**Notice of Changes**: We will notify users of material changes via email or in-app notification at least 30 days before the change takes effect.

---

## 13. Contact & Privacy Requests

### 13.1 Data Protection Officer (DPO)

For privacy inquiries or to exercise your rights:

**Email:** [INSERT PRIVACY_EMAIL]
**Mailing Address:** [INSERT MAGI OFFICE ADDRESS]
**Phone:** [INSERT CONTACT PHONE]
**Response Time:** 30 days

### 13.2 Privacy Request Form

Submit formal data subject requests via:
- Web form: [INSERT PRIVACY_REQUEST_URL]
- Email: [INSERT PRIVACY_EMAIL]
- Postal mail: [INSERT MAGI OFFICE ADDRESS]

### 13.3 Disputes & Complaints

If you believe we have violated your privacy rights:
1. **Contact Us**: Submit a complaint to our DPO
2. **Escalate**: File a complaint with your local data protection authority
3. **Legal Action**: Pursue legal remedies as applicable

---

## 14. Glossary

- **Personal Data**: Any information relating to an identified or identifiable person
- **Processing**: Any operation on data (collection, storage, use, deletion)
- **Controller**: The entity deciding why and how data is processed
- **Processor**: The entity processing data on behalf of the Controller
- **Data Subject**: The person to whom personal data relates
- **Sub-processor**: A processor engaged by the primary processor

---

## 15. Additional Resources

- [MAGI Terms of Service](./TERMS_OF_SERVICE.md)
- [MAGI Data Retention Policy](./DATA_RETENTION_POLICY.md)
- [MAGI Security Policy](./SECURITY_POLICY.md)

---

**This Privacy Policy is provided in English. For translations in other languages, please contact us.**

**Version:** 1.0 | **Last Updated:** March 19, 2026 | **Next Review:** March 19, 2027
