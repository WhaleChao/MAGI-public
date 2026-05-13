# MAGI Database Migrations

## Overview

This directory contains database migration scripts for MAGI schema evolution.

## Structure

```
migrations/
├── README.md           # This file
├── migrate.py          # Migration runner
└── versions/           # Ordered migration scripts
    └── 001_initial_schema.sql
```

## Usage

```bash
# Check current schema version
python migrations/migrate.py status

# Apply all pending migrations
python migrations/migrate.py upgrade

# Rollback last migration
python migrations/migrate.py rollback
```

## Adding a New Migration

1. Create a new file in `versions/` with the next sequence number:
   - Format: `NNN_description.sql`
   - Example: `002_add_user_roles.sql`

2. Include both UP and DOWN sections:
   ```sql
   -- UP
   ALTER TABLE users ADD COLUMN role VARCHAR(32) DEFAULT 'user';

   -- DOWN
   ALTER TABLE users DROP COLUMN role;
   ```

3. Test the migration on a staging environment before merging.
