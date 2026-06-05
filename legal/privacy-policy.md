# Privacy Policy — Panels

**Last updated: 2026-05-26**

Panels is a webcomic reader app developed by Russell Spencer ("I", "me"). This policy explains what data the app collects, how it is used, and your choices.

---

## What data is collected

### Data collected automatically

| Data | Purpose |
|------|---------|
| **Anonymous Firebase UID** | Every install gets a random anonymous identifier so your reading history can be backed up to the cloud. This ID contains no personal information. |
| **Reading history** | Which comics you have read (stored in Firestore under your anonymous UID). Used to sync your position across devices when you sign in with Google. |
| **Crash reports** (optional, on by default) | If the app crashes, an anonymised report is sent to Firebase Crashlytics. The report contains the crash stack trace, device model, OS version, and app version. It does not contain your name, email, or reading history. You can turn this off in Settings → Privacy. |

### Data you provide

| Data | When | Purpose |
|------|------|---------|
| **Google account** | Only if you choose "Sign in with Google" | Links your reading history to a real account so it can sync across multiple devices. Your email address is stored in Firebase Auth only to display it in Settings. |

---

## What data is NOT collected

- No advertising IDs
- No analytics SDKs (no Firebase Analytics, no Google Analytics)
- No device fingerprinting
- No location data
- No in-app tracking of browsing behaviour beyond which comics you mark as read

---

## Third-party services

| Service | Purpose | Privacy policy |
|---------|---------|----------------|
| **Firebase (Google)** | Authentication, cloud read-state sync, crash reporting | [firebase.google.com/support/privacy](https://firebase.google.com/support/privacy) |
| **GitHub** | Hosts the comic manifest files the app downloads | [docs.github.com/site-policy/privacy-policies](https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement) |

Comic images are loaded directly from the original publishers' servers (smbc-comics.com, xkcd.com, etc.). Those requests are subject to each publisher's own privacy policy.

---

## Data retention

- **Cloud read-state** is stored until you use "Delete My Data" in Settings, which wipes all Firestore data and generates a fresh anonymous account.
- **Crash reports** are retained by Firebase Crashlytics per their standard data retention policy (90 days).

---

## Your rights

You can:
- **Delete all your data** at any time via Settings → Account → Delete My Data.
- **Sign out** of Google and revert to anonymous mode at any time.
- **Disable crash reporting** via Settings → Privacy → Crash reporting.

---

## Comic content

Panels displays comics via RSS feeds and publicly available image URLs. All comic content is the property of the respective creators. If you are a creator and would like your work removed from this app, please contact **tueftlerapps@gmail.com** and it will be done promptly.

---

## Contact

Russell Spencer — **tueftlerapps@gmail.com**
