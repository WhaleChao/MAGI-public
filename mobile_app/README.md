# MAGI Mobile

Internal Android/iPhone wrapper for MAGI and Paperclip.

Configure the server at build time (always starts from the MAGI Mobile login entry):

```text
MAGI_MOBILE_APP_URL=https://your-public-host.example/mobile-app
```

If `MAGI_MOBILE_APP_URL` is not set, `scripts/configure_mobile_app.py` falls back
to `MAGI_PUBLIC_BASE_URL` or `MAGI_MOBILE_BASE_URL` and appends `/mobile-app`.
The checked-in Capacitor config intentionally uses `https://example.invalid` so
private Funnel or Tailnet hostnames do not ship in public releases.

## Android APK

This folder is prepared for Capacitor. Building an APK requires installing the npm dependencies and Android build tooling:

```bash
cd /Users/ai/Desktop/MAGI_v2/mobile_app
npm install
MAGI_MOBILE_APP_URL=https://your-public-host.example/mobile-app npm run prepare:config
npx cap add android
npm run build:android
```

The debug APK will be under:

```text
mobile_app/android/app/build/outputs/apk/debug/app-debug.apk
```

## iPhone

For internal use, the lowest-maintenance path is PWA:

1. Open your configured `MAGI_PUBLIC_BASE_URL` `/mobile` URL in Safari.
2. Use Share > Add to Home Screen.

If a signed IPA is needed later, use the same Capacitor config and add the iOS platform with an Apple Developer signing identity.
