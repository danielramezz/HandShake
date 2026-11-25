# Running Handshake

## Requirements
- Laptop with **Python 3.11+**. No internet is needed after the first install.
- All phones and the laptop on the **same Wi-Fi network**.

## Start the server
```
.\start.bat            (Windows terminal; or double-click start.bat in File Explorer)
./start.sh             (macOS / Linux)
```
The first run creates `.venv/` and installs `requirements.txt`. The terminal then prints:
```
  Phones on the same Wi-Fi open:  https://192.168.x.y:8443
  Live metrics dashboard:         https://192.168.x.y:8443/dashboard
  <QR code>
```
Manual alternative: `python -m venv .venv`, `.venv\Scripts\python -m pip install -r requirements.txt`, then `.venv\Scripts\python -m server.main`.

Options: `--port 9000` picks another port. `--http` turns off TLS for debugging only; end-to-end encryption then stops working on phones, because browsers only allow Web Crypto on HTTPS.

## iPhone setup (once per iPhone, about 1 minute, do it BEFORE demo day)
The server signs its HTTPS certificate with its own **Handshake Local CA**. iPhones need that CA installed and trusted; otherwise iOS may refuse the WebSocket (Protocol 1).
1. Start the server. Open `/dashboard` on the laptop, tap **iPhone: certificate**, and scan that QR code with the iPhone camera. It opens `https://<laptop-ip>:8443/ca.cer` in **Safari** (it must be Safari).
2. Safari says *This Connection Is Not Private*. Tap **Show Details → visit this website → Visit Website**.
3. *"This website is trying to download a configuration profile"*: tap **Allow**, then **Close**.
4. Open **Settings**. Tap **Profile Downloaded** near the top (or *General → VPN & Device Management*), then **Install** (passcode) → **Install** → **Done**.
5. **Settings → General → About → Certificate Trust Settings**: turn **on** *Handshake Local CA* → **Continue**.
6. Open the app QR code (`https://<laptop-ip>:8443`). There is no warning any more, and the padlock is normal.

- **Keep the `local/certs/` folder.** If the laptop's IP changes (another Wi-Fi or a hotspot), the server re-issues its certificate from the same CA by itself, and the phones stay trusted. Deleting `local/certs/` creates a new CA, and every phone must repeat the steps (remove the old profile first).
- Android (optional): Chrome works after tapping **Advanced → Proceed**. For full trust, install `ca.cer` under *Settings → Security → Encryption & credentials → Install a certificate → CA certificate*.
- **Safety net:** if a phone can reach the server but its WebSocket never opens, the app switches to **Compatibility mode** (HTTP + SSE) automatically and shows a message. Chat keeps working; reloading tries Fast mode (WebSocket) again.

## Remote access (optional: friends on another network)
Double-click **`share.bat`** (or run `.\start.bat --share` in a terminal).
```
.\start.bat --share
```
- The terminal (and the dashboard's **From anywhere** tab) shows a public link like `https://some-random-words.trycloudflare.com` plus a QR code. Anyone can open it from any network, with **no certificate setup**: Cloudflare's certificate is already trusted.
- The first time, `cloudflared.exe` (about 55 MB) is downloaded into `local/tools/`. If Avira blocks it, allow it.
- The link changes every time you start the server, and anyone who has it can create an account, so only share it with friends.
- Remote phones always use **Fast mode** (WebSocket). The free tunnel holds back Server-Sent Events, so Compatibility mode is greyed out remotely. On the Wi-Fi both modes work.
- **The graded demo uses the Wi-Fi (no `--share`).** Remote access is an extra.

## Install as an app (optional, looks great in the demo)
In Safari: **Share → Add to Home Screen**. Handshake gets its own icon and opens full-screen like a native app. The installed app has its own storage, so sign in once more inside it.

## Join from a phone
1. Scan the QR code from the terminal or from `/dashboard` on the laptop.
2. **Create account**, then pick a teammate from the list.
3. To make a chat secret: tap the **lock** in the chat header. Both people type the same chat password and check that the verification codes match.

## 5-minute demo script (practise it!)
1. *(before the TA arrives)* Server running, `/dashboard` open on the laptop, the certificate installed on every iPhone, all 4 phones signed in. Every phone shows a green dot with **Connected** under "Chats".
2. **All clients connected:** the dashboard shows 4 users and 4 sockets, each with its own client IP:port.
3. **Real-time, both directions:** A→B and B→A. Watch the ticks: ✓ stored on server → dim ✓✓ delivered to B's phone → bright ✓✓ when B opens the chat (read). While B types, A sees *typing…*. Tap a bubble → **Info** shows written → sent → delivered → read; open **Technical details** for the ACK time, protocol and bytes on the wire.
4. **Distinct chat logs:** A chats with B and with C separately.
5. **Offline sync:** turn Wi-Fi **off** on phone B. A sends 3 messages. Turn B's Wi-Fi back on: the 3 messages appear without a refresh, and a "Caught up on 3 messages" message shows.
6. **Graceful close:** close the browser on phone C. The dashboard log shows "disconnected", A↔B keep chatting, and C's chat header shows *last seen just now*.
7. **No loss / no duplicates:** with Wi-Fi off, B types a message (clock icon, "1 queued"), then reconnects. It is sent exactly once. *Duplicates rejected* on the dashboard counts retransmissions.
8. **Two protocols:** on B open **Settings → Advanced → Connection mode → Compatibility** (HTTP + SSE). A (Fast = HSP over WebSocket) and B still chat, and the dashboard shows the protocol per socket.
9. **E2EE:** turn on encryption between A and B and send a message. Show that the stored body is ciphertext: `GET /api/messages` in a browser on the laptop, signed in as A or B.
10. **Metrics:** dashboard tiles, messages/s chart, throughput chart.

## Troubleshooting
| Problem | Fix |
|---|---|
| Phones can't open the page | Windows Firewall: on the first run, allow Python on **Private** networks. Make sure the Wi-Fi network profile is *Private*, not *Public*. |
| Still can't connect | Many university and café networks use **client isolation** (devices can't reach each other). Use your own router, or turn on a **phone hotspot** and connect the laptop and all phones to it. |
| Wrong IP printed | The laptop has several adapters (VPN, virtual). Disconnect the VPN, or open the dashboard and use the IP of the Wi-Fi adapter (`ipconfig`). |
| iPhone shows "switched to Compatibility mode" or a certificate warning | The CA is not installed or not trusted yet: redo **iPhone setup**, especially step 5 (Certificate Trust Settings). |
| iPhone was set up, but the warning is back | `local/certs/` was deleted, so there is a new CA. Remove the old profile (*General → VPN & Device Management*) and redo iPhone setup. |
| "Encryption needs HTTPS" | The server was started with `--http`. Restart without it. |

## Tests and benchmarks
```
.venv\Scripts\python -m pytest                          # unit + end-to-end tests
.venv\Scripts\python -m benchmarks.bench latency        # metric 1 + 2
.venv\Scripts\python -m benchmarks.bench scale          # metric 3 (--clients 2,10,25,50)
.venv\Scripts\python -m benchmarks.bench crypto         # E2EE vs plaintext
```
For real Wi-Fi numbers, run the benchmark on a **second laptop** on the same Wi-Fi with `--server https://<server-ip>:8443`.

## Reset
Stop the server and delete `local/data/` (all accounts and messages) and/or `local/certs/` (a new certificate is created on the next start).
