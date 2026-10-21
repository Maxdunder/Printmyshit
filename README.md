# Print Files: upload a .3mf, send it to the printer

How it fits together:

```
You (phone or PC)                     GitHub repo (this site)         Your home
  web page ── upload .3mf ──────────► models/cube.3mf
  web page ── "print cube.3mf" ──► MQTT broker ──► ESP32 ──► downloads from the repo
                                                     └────► uploads to the A1 (FTPS) ──► starts it
  web page ◄── printer state ◄──── MQTT broker ◄──── ESP32 (reads the A1 over local MQTT)
```

The printer must be in **LAN Only Mode with Developer mode on**.

## 1. Put the site online (GitHub Pages)

1. Create a **public** repository, for example `printfiles`.
2. Upload everything in this folder to the repository root (`index.html`, `models/`, `incoming/`, `slicer/`, `.github/`, `.nojekyll`).
   The folder `.github/workflows/slice.yml` must end up at exactly that path. If dragging the `.github` folder
   does not work, use **Add file > Create new file**, type `.github/workflows/slice.yml` as the name, and paste the file's text.
   Do the same for `slicer/slice.py` if needed.
3. **Settings > Pages**: Deploy from a branch, `main`, folder `/ (root)`. After a minute the site is at
   `https://YOURNAME.github.io/printfiles/`.

## 2. Create a GitHub token (so the page can upload files)

1. GitHub > Settings > Developer settings > **Personal access tokens > Fine-grained tokens > Generate new token**.
2. Repository access: **Only select repositories**, pick your repo.
3. Permissions: **Contents: Read and write** and **Actions: Read and write**. Nothing else.
4. Set an expiry and copy the token (it starts with `github_pat_`).

The token is pasted into the page's Settings and stays in that browser only. Never commit it to the repo.

## 3. Create a free MQTT broker (EMQX Cloud Serverless)

The free quota is 1,000,000 session minutes and 1 GB of traffic per month. An always-on ESP32 uses about
43,000 session minutes a month and a few MB of traffic, so this stays free.

1. Sign up at emqx.com/en/cloud and open the console. Create a project if it asks for one.
2. **New Deployment** > plan **Serverless**. Leave the monthly **Spend Limit** at `0` (free quota only).
   Name it, accept the terms, click **Deploy**, and wait for the status **Running**.
3. Open the deployment. On **Overview**, under **Connection Information**, copy the **Address**.
   - ESP32: this address, port `8883` (MQTT over TLS).
   - Web page: `wss://ADDRESS:8084/mqtt` (WebSocket over TLS).
4. **Access Control > Authentication > Add**: create two username/password pairs, one for the ESP32 and one for the web page.
5. Choose a long random **topic prefix**, for example `ams-x4r9m2q7k1`. It acts like a password for the topics.

## 4. Configure the ESP32 (`esp32_bridge.ino`)

- WiFi and printer values, as before.
- `CLOUD_HOST` = the Address, `CLOUD_PORT` = `8883`, `CLOUD_USER` and `CLOUD_PASS` = the ESP32 login, `TOPIC_PREFIX` = your prefix.
- `ALLOWED_URL_PREFIX = "https://raw.githubusercontent.com/YOURNAME/YOURREPO/"`.
  The ESP32 refuses to download from anywhere else.

## 5. Configure the page

Open the site, expand **Settings**, and enter: owner, repository, branch, the GitHub token, the broker WebSocket URL, the web login, and the same topic prefix. Press **Save and connect**.

The chips at the top turn green. The printer card shows the live state.

## Slicing a model online

1. In the **Slice a model** card, choose an `.stl` or an unsliced `.3mf`.
2. Pick the material (PLA, PETG, ABS, TPU), layer height and infill, then press **Slice on GitHub**.
3. The page uploads the model to `incoming/` and starts the **Slice** job in your repo's Actions tab. It uses OrcaSlicer
   with the Bambu Lab A1 0.4 nozzle profiles. ABS adds an 8 mm brim.
4. After 1 to 3 minutes the sliced file `name_MATERIAL.gcode.3mf` appears in **Files** and the original model is removed from `incoming/`.
5. Press **Upload & print**.

If the job fails, the page shows a link to the log. Copy the log text if you need help.

In the repo, go to Settings > Actions > General and make sure Actions are allowed. The job needs no other setup.

## Using it

- **Upload a file:** drop a sliced `.3mf` on the page (File > Export > Export plate sliced file in Bambu Studio). Names are cleaned to letters, digits, `.`, `_` and `-`.
- **Upload only:** the ESP32 downloads the file and puts it on the printer's storage.
- **Upload & print:** the same, then it starts the print. Only works when the printer is idle.
- **Pause / Resume / Stop:** sent straight to the printer.

Start with `hello.txt` and **Upload only** to check the connection before using filament.

## Safety notes

- The ESP32 only accepts file names made of safe characters, only `.3mf` for printing, only URLs from your repo, and refuses to start a job while the printer is running or paused.
- A remote start still means an unattended printer. Only start prints with a clear bed and filament you trust.
- Anyone who has a login for your broker and knows the prefix can send commands. Keep both private, and delete the web login in the broker console if a device is lost.
