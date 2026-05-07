# Implementation Plan - Balthasar Discord Migration

## Goal
Switch Balthasar's communication channel from WhatsApp (NanoClaw default) to Discord, allowing him to participate in a "Unified Debate Channel" with the other Magi nodes. Enable Web UI configuration for Discord credentials.

## User Review Required
> [!IMPORTANT]
> **Architecture Shift**: Balthasar will now primarily listen and speak on Discord. The WhatsApp module will be sidelined. The User must provide a **Discord Webhook URL** (for broadcasting) and a **Bot Token** (for listening/replying) via the Dashboard.

## Proposed Changes

### 1. Dependencies
*   [NEW] Install `discord.js` and `body-parser` (for Web UI form handling).

### 2. Dashboard Upgrade (`src/dashboard.ts`)
*   [MODIFY] Add a "Settings" section to the Web UI.
*   [NEW] Add API endpoints `/save-config` to store:
    *   `DISCORD_WEBHOOK_URL`: For Nightly Council broadcasts.
    *   `DISCORD_BOT_TOKEN`: For conversational interface.
    *   `DISCORD_CHANNEL_ID`: The "Debate Chamber" channel ID.

### 3. The Bridge (`src/discord_bridge.ts`)
*   [NEW] Implement a simple Discord Client using `discord.js`.
*   [NEW] Replace `src/index.ts` startup logic to initialize Discord instead of WhatsApp (or alongside it).

### 4. Nightly Council (`src/nightly_council.ts`)
*   [MODIFY] Update the `runNightlyCouncil` function to:
    1.  Read `DISCORD_WEBHOOK_URL` from config.
    2.  Post the "Proposal Evaluation" and "Vote" directly to the Discord channel using the Webhook.
    3.  Format the message as a rich Embed (Title: 🦅 Balthasar's Verdict).

## Verification Plan

### Manual Verification
1.  **Dashboard**: Open `http://localhost:5001`, enter a test Webhook URL.
2.  **Council Test**: Click "Trigger Nightly Vote" on Dashboard.
3.  **Observation**: Verify a message appears in the user's Discord channel.
