# LoomChat

A small Matrix client made for talking to Loom agents from a phone: one room per
agent, rooms waiting for your answer at the top, quick replies, notes and a
"free" flag per room, images and files.

It is a single `index.html` (`src/assets/`). Two ways to run it:

- **Web**: `docker compose -f docker-compose.chat.yml up -d` at the repo root
  serves it next to a Conduit homeserver (see the comments in that file).
- **Android**: `ANDROID_SDK=/path/to/sdk ./build.sh` builds a signed APK
  without Gradle (needs platform 34 and build-tools 35.0.0). The app is a
  WebView around the same page; enter your homeserver's HTTPS URL at login.

## Optional Loom API

Agent status, quota bars, the live terminal view and the Stop / Compact /
Clear buttons need a separate "Loom API" that is not part of this repo yet.
Without it, those controls stay hidden. To point the page at one, set
`localStorage.lapi` to its base URL.
