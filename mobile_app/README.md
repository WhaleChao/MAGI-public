# MAGI Mobile

Internal Android/iPhone wrapper for MAGI and Paperclip.

Default server (always starts from the MAGI Mobile login entry):

```text
https://aimac-mini.tail6738b7.ts.net/mobile-app
```

This uses the verified Tailscale Funnel HTTPS endpoint. If public DNS is still
propagating, `scripts/mobile_app_status.py` also reports a Tailnet fallback URL
for phones already connected to the MAGI Tailnet.

## Android APK

This folder is prepared for Capacitor. Building an APK requires installing the npm dependencies and Android build tooling:

```bash
cd /Users/ai/Desktop/MAGI_v2/mobile_app
npm install
npx cap add android
npm run build:android
```

The debug APK will be under:

```text
mobile_app/android/app/build/outputs/apk/debug/app-debug.apk
```

## iPhone

For internal use, the lowest-maintenance path is PWA:

1. Open `https://aimac-mini.tail6738b7.ts.net/mobile` in Safari.
2. Use Share > Add to Home Screen.

If a signed IPA is needed later, use the same Capacitor config and add the iOS platform with an Apple Developer signing identity.
