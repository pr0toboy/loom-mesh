# LoomChat

A small Matrix client made for talking to Loom agents from a phone: one room per
agent, rooms waiting for your answer at the top, quick replies, notes and a
"free" flag per room, images and files.

It is a single `index.html` (`src/assets/`). Two ways to run it:

- **Web**: at the repo root,
  `cp .env.chat.example .env.chat` (set `LOOM_REG_TOKEN`), then
  `docker compose -f docker-compose.chat.yml --env-file .env.chat up -d`
  serves it next to a Conduit homeserver. Open the page, tap "Create an
  account" and enter the registration token. The first account created
  becomes the Conduit server admin.
- **Android**: `ANDROID_SDK=/path/to/sdk ./build.sh` builds a signed APK
  without Gradle (needs platform 34 and build-tools 35.0.0). The app is a
  WebView around the same page; enter your homeserver's HTTPS URL at login.

## Optional Loom API

Agent status, quota bars, the live terminal view and the Stop / Compact /
Clear buttons need a separate "Loom API" that is not part of this repo yet.
Without it, those controls stay hidden.

Rooms: LoomChat lists the rooms you are in and accepts invitations
automatically. It does not create rooms; your agents (or the Matrix bridge)
invite you, or use any other Matrix client to create one. To point the page at one, set
`localStorage.lapi` to its base URL. Note that your Matrix access token is
sent to that URL as a Bearer token, so only point it at a server you run.
