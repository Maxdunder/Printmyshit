# Print Files: upload a .3mf, send it to the printer

How it fits together:

```
You (phone or PC)                     GitHub repo (this site)         Your home
  web page ── upload .3mf ──────────► models/cube.3mf
  web page ── "print cube.3mf" ──► broker (HiveMQ) ──► ESP32 ──► downloads from the repo
                                                         └────► uploads to the A1 (FTPS) ──► starts it
  web page ◄── printer state ◄──── broker ◄──────────── ESP32 (reads the A1 over local MQTT)
```

The printer must be in **LAN Only Mode with Developer mode on**.

## 1. Put the site online (GitHub Pages)

1. Create a **public** repository, for example `printfiles`.
2. Upload everything in this folder to the repository root (`index.html`, `models/`, `.nojekyll`).
3. **Settings > Pages**: Deploy from a branch, `main`, folder `/ (root)`. After a minute the site is at
   `https://YOURNAME.github.io/printfiles/`.

## 2. Create a GitHub token (so the page can upload files)

1. GitHub > Settings > Developer settings > **Personal access tokens > Fine-grained tokens > Generate new token**.
2. Repository access: **Only select repositories**, pick `printfiles`.
3. Permissions: **Contents: Read and write**. Nothing else.
4. Set an expiry you are comfortable with and copy the token (it starts with `github_pat_`).

The token is pasted into the page's Settings and stays in that browser only. Never commit it to the repo.

## 3. Create a free MQTT broker (HiveMQ Cloud)

1. Sign up at hivemq.com/mqtt-cloud-broker and create a free serverless cluster.
2. Note the hostname, for example `abc123.s1.eu.hivemq.cloud`.
3. **Access management**: create two credentials with *Publish and Subscribe* permission: one for the ESP32, one for the web page (so you can revoke the page's separately).
4. Choose a long random **topic prefix**, for example `ams-k7f3q92x`. It acts like a password for the topics, so don't share it.

Ports: the ESP32 uses `8883` (MQTT over TLS), the web page uses `wss://HOSTNAME:8884/mqtt` (WebSocket).

## 4. Configure the ESP32 (`esp32_bridge.ino`)

Fill in the settings at the top:

- WiFi and printer values, as before.
- `CLOUD_HOST`, `CLOUD_USER`, `CLOUD_PASS` (the ESP32 credential), `TOPIC_PREFIX`.
- `ALLOWED_URL_PREFIX = "https://raw.githubusercontent.com/YOURNAME/printfiles/"`.
  The ESP32 refuses to download from anywhere else.

## 5. Configure the page

Open the site, expand **Settings**, and enter: owner, repository, branch, the GitHub token, the broker WebSocket URL, the web credential, and the same topic prefix. Press **Save and connect**.

The chips at the top should turn green. The printer card shows the live state.

## Using it

- **Upload a file:** drop a sliced `.3mf` on the page (File > Export > Export plate sliced file in Bambu Studio). Names are cleaned to letters, digits, `.`, `_` and `-`.
- **Upload only:** the ESP32 downloads the file and puts it on the printer's storage.
- **Upload & print:** the same, then it starts the print. Only works when the printer is idle.
- **Pause / Resume / Stop:** sent straight to the printer.

Start with `hello.txt` and **Upload only** to check the connection before using filament.

## Safety notes

- The ESP32 only accepts file names made of safe characters, only `.3mf` for printing, only URLs from your repo, and refuses to start a job while the printer is running or paused.
- A remote start still means an unattended printer. Only start prints with a clear bed and filament you trust.
- Anyone who can publish to your broker with the right prefix can send commands. Keep the credentials and prefix private. Revoke the web credential in HiveMQ if a device is lost.
