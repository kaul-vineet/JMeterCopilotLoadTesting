# Copilot Studio Load Test

A load testing tool for Microsoft Copilot Studio bots. It simulates many users having real conversations with your bot at the same time, measures how fast the bot responds, and tells you whether it can handle your target load.

---

## Table of Contents

1. [What this tool does](#1-what-this-tool-does)
2. [Architecture](#2-architecture)
3. [Prerequisites — what you need before starting](#3-prerequisites)
4. [Azure setup — step-by-step](#4-azure-setup)
5. [Copilot Studio setup](#5-copilot-studio-setup)
6. [First-time installation](#6-first-time-installation)
7. [Running the setup wizard](#7-running-the-setup-wizard)
8. [Writing test scripts (utterance files)](#8-writing-test-scripts-utterance-files)
9. [Running the load test](#9-running-the-load-test)
10. [Reading the results](#10-reading-the-results)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. What this tool does

Your Copilot Studio bot is designed to handle one user at a time from the browser. Before going to production, you need to know: what happens when 50 users are talking to it simultaneously? Does it slow down? Does it fail?

This tool answers that question by:

- **Simulating multiple users at once.** Each simulated user follows a script — a list of messages to send — and behaves like a real person (it pauses between messages the way a human would).
- **Automating authentication.** Copilot Studio bots protected by Microsoft sign-in require a real user identity to respond. This tool authenticates each simulated user using a real Microsoft 365 account, the same way a browser would — but fully automated, so you can run 50 users without 50 browser windows.
- **Measuring response time.** The tool records exactly how long the bot takes to reply to each message, from the moment the message is sent to the moment the first response arrives.
- **Producing a report.** At the end of a test run, you get a dashboard showing requests per second, average response time, 95th-percentile response time, and error rate — the same metrics used in official Microsoft Copilot Studio performance benchmarks.

---

## 2. Architecture

Understanding how the pieces fit together will help you set it up correctly and debug issues when they arise.

### 2.1 The DirectLine protocol

Copilot Studio bots are not chat programs you connect to directly. They speak a protocol called **Bot Framework DirectLine**, which is a Microsoft-designed API layer that sits between your application and the bot.

When you talk to a bot on a website, the website is secretly using DirectLine. This tool does the same thing programmatically.

A DirectLine conversation works in two directions at once:
- **Sending messages (HTTP POST):** You send a message to the bot by making an HTTP POST request to the DirectLine REST API.
- **Receiving replies (WebSocket):** The bot's replies come back over a WebSocket — a persistent, two-way connection that stays open for the duration of the conversation. The tool listens on this WebSocket and records when replies arrive.

This split is why the tool makes both HTTP calls and WebSocket connections. They are different channels doing different jobs.

```
  Load Test Tool                         Microsoft Cloud
  ─────────────────────────────────────────────────────────
  HTTP POST /activities  ──────────────►  DirectLine API
                                                │
                                                ▼
  WebSocket (listening) ◄──────────────  Bot replies (streaming)
                                                │
                                          Copilot Studio Bot
```

### 2.2 Authentication — why it is complex

Most load testing tools just send HTTP requests. This bot requires the user to be signed in to a Microsoft account before it will respond to questions. That means every simulated user needs a real token — a cryptographic proof of identity issued by Microsoft Entra ID (formerly called Azure Active Directory).

Getting that token requires a two-application pattern that Microsoft requires for security reasons:

**Application 1 — the Resource (CopilotStudioAuthApp):**
Copilot Studio creates this automatically in your Azure tenant when you configure authentication. It represents the bot's identity. It exposes a permission called `access_as_user`. When a user signs in, they are granting your tool the right to "access as user" on this resource.

**Application 2 — the Client (your load test app):**
This is an app registration you create yourself. It represents the load test tool. It requests the `access_as_user` permission from Application 1 on behalf of each test user.

Think of it like a door with two keys. The bot (Application 1) has the lock. Your load test client (Application 2) has a key. To open the door, you need the right key for that specific lock.

**Token exchange:**
When the bot wants proof of identity, it sends the load test tool an "OAuthCard" — a card saying "I need you to prove who you are." The tool responds by sending the user's access token back through the DirectLine channel. This exchange is called `signin/tokenExchange`. If the token has the correct audience and scope, the bot accepts it and continues the conversation.

```
  1. Tool sends "hi"
  2. Bot replies with OAuthCard: "Please authenticate"
  3. Tool sends token exchange: "Here is the user's signed token"
  4. Bot validates the token and replies: "Hello! How can I help you?"
  5. Conversation continues normally
```

### 2.3 Token caching

Acquiring a new token requires the user to go to a website and enter a code (the "device code flow"). This cannot be done 50 times in a loop during a test. So the tool caches tokens on disk, encrypted with a key stored in Windows Credential Manager.

Before a test runs, you authenticate each profile once through the terminal. The token (and its refresh token, which lets the tool renew it silently) is saved encrypted in `profiles/.tokens/`. During the test, the tool reads these cached tokens. If a token has less than 10 minutes of life left, it uses the refresh token to get a new one automatically.

### 2.4 Profiles — simulating multiple users

A "profile" is a test user account. Each profile has:
- A username (a Microsoft 365 account email address, e.g. `loadtest.user1@yourcompany.com`)
- A display name (shown in the terminal during the test)
- Optionally, a scenario name (see section 2.5)

Each profile corresponds to a real account in your Microsoft 365 tenant. The bot will see these as real signed-in users.

Why multiple profiles? A single user account is typically rate-limited by the bot. Spreading the load across multiple accounts makes the test more realistic and avoids single-account throttling.

### 2.5 Scenarios — what messages to send

A scenario is a CSV (comma-separated values) file containing a list of messages. You put these files in the `utterances/` folder. For example, `utterances/it_support.csv` might contain messages like "How do I reset my password?" and "I can't access my email."

Each scenario becomes a separate set of virtual users in Locust. If you have three CSV files, you get three "user types" that can be run independently or together.

Profiles can be pinned to specific scenarios. If you have two profiles and two scenarios, you can say "Profile A always uses the HR scenario" and "Profile B always uses the IT support scenario." Profiles without a pinned scenario are assigned to scenarios by position (round-robin).

### 2.6 How Locust drives the test

Locust is a Python-based load testing framework. This tool uses Locust as its execution engine. Here is what Locust does during a test:

1. **Spawning:** You tell Locust to start, say, 10 users at a rate of 1 per second. Every second, Locust creates a new virtual user (called a "User" in Locust terminology) until there are 10.

2. **Each user's lifecycle:**
   - **`on_start`:** The user opens a DirectLine conversation and WebSocket.
   - **`@task` (the main loop):** The user picks the next utterance from its CSV, sends it to the bot, waits for a reply, then waits for a "think time" (30–60 seconds by default) before sending the next message.
   - **`on_stop`:** The user closes the WebSocket when the test ends.

3. **When all utterances are exhausted:** The user closes the current conversation and opens a fresh one, then starts cycling through the utterances again from the beginning.

4. **Metrics:** Every time the tool receives a bot reply, it reports the latency to Locust using `events.request.fire()`. Locust aggregates these into statistics you can see in real time on its web dashboard.

### 2.7 The startup sequence

Running `python run.py` does the following before any load test traffic is sent:

```
python run.py
    │
    ├─ 1. Wizard (if not yet configured)
    ├─ 2. Credential check (reads from Windows Credential Manager)
    ├─ 3. Profile status (checks cached tokens are valid)
    ├─ 4. Authentication (device code flow for any profile that needs it)
    ├─ 5. Pre-flight bot check (sends "hi" to the bot, verifies it responds)
    ├─ 6. Countdown animation
    └─ 7. Launches Locust subprocess → web UI at http://localhost:8089
```

This design means the Locust web interface only appears after everything is confirmed working. You will not see "Starting web interface" until the bot has been verified.

---

## 3. Prerequisites

Before you start, make sure you have the following:

| Requirement | Notes |
|---|---|
| **Windows 10/11 machine** | Required for Windows Credential Manager integration. Linux works with an extra step (see `TOKEN_ENCRYPTION_PASSWORD` in `.env.example`). |
| **Python 3.10 or newer** | Download from python.org. During installation, tick "Add Python to PATH". Version 3.10 is the minimum — the tool uses type syntax introduced in that version. |
| **A published Copilot Studio bot** | The bot must be published and have the Direct Line channel enabled. |
| **Two test user accounts** | Real Microsoft 365 accounts in your tenant (e.g. `loadtest.user1@yourcompany.com`). These accounts will be used as simulated users. They need a Copilot Studio licence or a Teams licence. |
| **Azure portal access** | You need permission to register applications in Microsoft Entra ID. The "Application Developer" role is sufficient. |
| **The bot's DirectLine Secret or Token Endpoint URL** | From Copilot Studio → Settings → Channels → Direct Line. |
| **The bot's OAuth Connection Name** | Visible in Copilot Studio → Settings → Security → Authentication. Only needed if the bot uses authentication. |

---

## 4. Azure Setup

This section walks through everything you need to create in Azure to make authenticated load testing work. If your bot does **not** use authentication (it is a public bot anyone can use without signing in), skip to Section 6.

### 4.1 Understand the goal

You are creating one Azure App Registration that represents the load test tool. The Copilot Studio bot already has its own App Registration (created automatically when you configure authentication in Copilot Studio). You need to connect them.

### 4.2 Find the bot's existing App Registration (the Resource App)

1. Sign in to the Azure portal: https://portal.azure.com
2. In the search bar at the top, type **Microsoft Entra ID** and click on it.
3. In the left menu, click **App registrations**.
4. Click the **All applications** tab.
5. Search for an app that includes "CopilotStudio" or your bot's name in its name. It was created automatically by Copilot Studio.
6. Click on that app and copy its **Application (client) ID**. It looks like `a172951c-2123-4f0a-9a63-3c5477d034d5`. Save this as your `AGENT_APP_ID` — you will need it in the setup wizard.

> **Shortcut:** In Copilot Studio → Settings → Security → Authentication, there is usually a link called **View application** or the Client ID is shown directly. Copy that Client ID and use it to search in Azure portal instead of browsing the app list.

> **How to confirm it is the right app:** In the app's left menu, click **Expose an API**. You should see a scope listed that ends in `/access_as_user`. If you see that, this is the right app.

### 4.3 Create the Load Test Client App Registration

This is the new app you are creating to represent the load test tool.

1. In Microsoft Entra ID → App registrations, click **New registration**.
2. Give it a name like `CopilotStudio-LoadTest-Client`.
3. Under **Supported account types**, choose **Accounts in this organizational directory only**.
4. Leave the Redirect URI blank.
5. Click **Register**.
6. On the overview page, copy the **Application (client) ID**. Save this as your `CLIENT_ID`.
7. Also copy the **Directory (tenant) ID** from the same page. Save this as your `TENANT_ID`.

### 4.4 Make the load test app a "public client"

The load test tool uses a flow called "device code flow" where the user approves sign-in on a separate device/browser. This flow requires the app to be configured as a public client.

1. In your new app registration, click **Authentication** in the left menu.
2. Scroll down to **Advanced settings**.
3. Under **Allow public client flows**, toggle **Enable the following mobile and desktop flows** to **Yes**.
4. Click **Save**.

### 4.5 Grant the load test app permission to call the bot's resource app

1. In your new app registration, click **API permissions** in the left menu.
2. Click **Add a permission**.
3. In the panel that opens, click the **APIs my organization uses** tab.
4. Search for the name of the bot's resource app (the one you found in Step 4.2, e.g. `CopilotStudioAuthApp`).
5. Click on it.
6. Under **Delegated permissions**, tick `access_as_user`.
7. Click **Add permissions**.
8. Back on the API permissions page, click **Grant admin consent for [your organisation]** and confirm. This step requires a Global Administrator or Privileged Role Administrator.

> **What admin consent means:** By granting admin consent, an administrator approves this permission on behalf of every user in the organisation. This means individual test users will NOT be shown a "Do you allow this app to access...?" pop-up when they sign in — the administrator has pre-approved it. Without admin consent, each user's first sign-in would require them to manually approve the permission in a browser, which defeats the purpose of automation.

> **What "delegated permissions" means:** The load test tool will act **on behalf of** a signed-in user. It does not act as itself. This is "delegated" access — the user's rights are delegated to the tool. This is distinct from "application permissions" where an app acts entirely on its own authority.

### 4.6 Verify the scope

After completing section 4.5, the load test tool will request this specific OAuth 2.0 scope when signing in:

```
api://<AGENT_APP_ID>/access_as_user
```

Where `<AGENT_APP_ID>` is the client ID you copied in step 4.2. The tool fills this in automatically.

---

## 5. Copilot Studio Setup

### 5.1 Enable the Direct Line channel

Direct Line is the API channel this tool uses to talk to the bot.

1. Open Copilot Studio and select your bot.
2. Go to **Settings → Channels**.
3. Click **Direct Line**.
4. If it is not already enabled, enable it.
5. Under **Secret keys**, click **Show** next to one of the keys and copy it. Save this as your `DIRECTLINE_SECRET`.

> **Important:** Keep the DirectLine Secret private. Anyone with this secret can send messages to your bot and consume your bot's capacity. Do not commit it to version control.

### 5.2 Confirm the bot's authentication mode

1. In Copilot Studio, go to **Settings → Security → Authentication**.
2. Check whether authentication is set to "No authentication", "Authenticate with Microsoft", or "Authenticate manually".

- **No authentication:** The bot is public. Skip sections 4.1–4.6 and leave `AGENT_APP_ID` blank in the wizard.
- **Authenticate with Microsoft:** Uses Entra ID SSO. You must complete all of Section 4.
- **Authenticate manually:** Also uses Entra ID. You must complete all of Section 4.

3. If authentication is enabled, note the **Client ID** shown on this page. This is the `AGENT_APP_ID` you need.

---

## 6. First-time Installation

### 6.1 Get the code

```
git clone https://github.com/kaul-vineet/JMeterCopilotLoadTesting.git
cd JMeterCopilotLoadTesting
```

> **New machine / new clone:** If you clone this repository to a different machine, you will need to re-run the setup wizard and re-authenticate each profile. Credentials and tokens are stored in Windows Credential Manager and `profiles/.tokens/` on the local machine — they are intentionally not committed to the repository (they would be a security risk if they were).

### 6.2 Create a Python virtual environment

A virtual environment keeps this project's dependencies isolated from other Python programs on your machine. This prevents version conflicts.

```
python -m venv .venv
.venv\Scripts\activate
```

After running `activate`, your terminal prompt will show `(.venv)` at the start. All Python commands from now on will use this isolated environment.

> **PowerShell note:** If you see an error saying "running scripts is disabled on this system", run this once in PowerShell as your normal user (not administrator):
> ```
> Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
> ```
> Then try `.venv\Scripts\activate` again. This is a Windows security policy — the command above allows scripts you create on your own machine to run.

### 6.3 Install dependencies

```
pip install -r requirements.txt
```

This installs:
- **locust** — the load testing framework
- **msal** — Microsoft Authentication Library, handles the device code sign-in flow
- **requests** — HTTP client used for DirectLine REST calls
- **websocket-client** — used to receive bot replies over WebSocket
- **cryptography** — Fernet encryption for token storage
- **keyring** — reads/writes Windows Credential Manager
- **rich** — the coloured terminal output
- **colorama** — Windows console colour compatibility

### 6.4 Create your utterance files

Before running the wizard, put at least one CSV file in the `utterances/` folder. See Section 8 for the format. There is an example file `utterances/it_support.csv` already included.

---

## 7. Running the Setup Wizard

Run:

```
python run.py
```

The first time you run this (or if not configured), the setup wizard opens automatically. The wizard saves all credentials into **Windows Credential Manager** — the same secure store that browsers use to save passwords. Nothing sensitive is written to any file.

### 7.1 The wizard menu

The wizard shows a numbered list of settings:

```
  [1]  Tenant ID                          (not set)
  [2]  App Registration Client ID         (not set)
  [3]  Bot Client ID (SSO)                (not set — SSO disabled)
  [4]  DirectLine Secret                  (not set)
  [5]  Token Endpoint URL                 (not set)
  [6]  Add profile
```

Type a number and press Enter to edit that field. Press Enter with no number to save and continue.

### 7.2 Field-by-field guide

**[1] Tenant ID**
The unique identifier for your Microsoft 365 organisation in Azure.

Where to find it: Azure portal → Microsoft Entra ID → Overview → Tenant ID.

It looks like: `72f988bf-86f1-41af-91ab-2d7cd011db47`

---

**[2] App Registration Client ID**
The identifier of the load test client app you created in Section 4.3.

Where to find it: Azure portal → App registrations → [your app] → Application (client) ID.

It looks like: `cea29e59-7415-4281-9c05-8fefbac4f1b1`

---

**[3] Bot Client ID (SSO)**
The identifier of the bot's resource app (the one Copilot Studio created automatically).

Where to find it: Copilot Studio → Settings → Security → Authentication → Client ID.
Alternatively: Azure portal → App registrations → All applications → search for your bot's auth app → Application (client) ID.

It looks like: `a172951c-2123-4f0a-9a63-3c5477d034d5`

Leave this blank if your bot does not use authentication.

---

**[4] DirectLine Secret**
The secret key that gives this tool permission to talk to your bot.

Where to find it: Copilot Studio → Settings → Channels → Direct Line → Secret keys → Show.

The value is a long string of random characters. It is masked as you type it.

---

**[5] Token Endpoint URL**
An alternative to the DirectLine Secret. Some organisations use a Token Endpoint — a URL on their own server that vends temporary DirectLine tokens without exposing the raw secret.

Where to find it: Copilot Studio → Settings → Channels → Direct Line → Token Endpoint URL.

If you set this, leave the DirectLine Secret blank (or vice versa). If both are set, the Token Endpoint takes priority.

---

**Profiles**
After the credential fields, you will be prompted to add at least one profile. A profile is a test user account.

For each profile you need:
- **Username (UPN):** The full email address of the test account, e.g. `loadtest.user1@yourcompany.com`
- **Display name:** A short label shown in the terminal (e.g. `User 1`). Press Enter to accept the default (the part before @).
- **Scenario (CSV name):** The name of the CSV file (without `.csv`) this profile will use. Leave blank to auto-assign.

After adding profiles, the wizard asks whether to add more. When done, press Enter with no number to save.

### 7.3 Authentication

After saving, the wizard checks whether each profile already has a valid cached token. For any that do not, it starts the device code flow:

```
  1. Open:  https://login.microsoft.com/device
  2. Enter: EL4LXCF6H

  Waiting for sign-in...
```

Open a browser, go to that URL, enter the code shown, and sign in with the test user account. The tool waits. When sign-in completes, the token is saved encrypted to `profiles/.tokens/`. You will not need to do this again for that account until the refresh token expires (typically 90 days).

### 7.4 Pre-flight check

Before showing the Locust web interface, the tool sends "hi" to the bot and waits for a reply (up to 15 seconds). This confirms the credentials work and the bot is reachable. If this fails, an error message explains what to check.

> **Note:** The pre-flight check sends one real message to your bot. If you are testing a production bot, this will appear in the bot's analytics as one conversation. This is unavoidable and harmless — it is a single "hi" message sent once at startup.

---

## 8. Writing Test Scripts (Utterance Files)

Utterance files are CSV files in the `utterances/` folder. Each file is one "scenario" — one type of user journey through the bot.

### 8.1 Format

The file must have a header row with the column name `utterance`. Each subsequent row is one message to send.

```csv
utterance
Hi, I need help with my password.
I can't log in to my email.
What are the steps to reset a password?
Please escalate this to a human.
```

The tool sends these messages in order. After the last message, it opens a new conversation and starts again from the first message.

### 8.2 What makes a good test script

- **Cover the full journey.** Include the greeting, the main questions, and the closing. A realistic conversation has 5–10 turns.
- **Include escalations and edge cases.** Test what happens when the bot is asked something it does not know, or when the user asks to speak to a human.
- **Match real usage patterns.** If analytics show most users ask 3 questions per session, keep scripts to 3 utterances.
- **One script per scenario.** If your bot handles both HR and IT topics, create `utterances/hr.csv` and `utterances/it_support.csv` separately.

### 8.3 Assigning profiles to scenarios

If you have two CSV files and two profiles, assign each profile to one scenario in the wizard (the "Scenario" field when adding a profile). The profile's username will be used exclusively for that scenario's virtual users.

If you have more CSV files than profiles, profiles are reused in rotation across scenarios.

---

## 9. Running the Load Test

### 9.1 Start the tool

With the virtual environment active:

```
python run.py
```

If already configured, the wizard is skipped. The tool checks credentials, verifies profile tokens, runs the pre-flight check, then opens the Locust web interface.

Open your browser and go to: **http://localhost:8089**

### 9.2 The Locust web interface

At the top of the page you will see a "Copilot Studio Test Configuration" panel with these fields:

**DirectLine Connection:**
- **DirectLine Secret / Token Endpoint URL:** Pre-filled from your saved configuration. You can override them per-run here without re-running the wizard.

**Timing:**
- **Response Timeout (seconds):** How long the tool waits for a bot reply before recording an error. Default: 10 seconds. Increase this if your bot is slow.
- **Think Time Min/Max (seconds):** The pause between messages. The tool picks a random wait between Min and Max. Default: 30–60 seconds. Microsoft Copilot Studio performance guidelines recommend 30–60 seconds of think time to simulate realistic usage.

**Success Criteria:**
- **95th Percentile Target (ms):** The response time benchmark you are testing against. Microsoft's published baseline is 2000ms (2 seconds) at the 95th percentile.
- **Max Error Rate (%):** The acceptable failure rate. Microsoft's baseline is 0.5%.

**The standard test parameters (scroll down):**
- **Number of users:** How many simulated users to run concurrently.
- **Spawn rate:** How many new users to start per second.
- **Host:** Leave this as the default or blank — the tool fills it automatically.

### 9.3 Starting the test

Set the number of users and spawn rate, then click **Start swarming**. The configuration panel settings are sent to the tool first, then the test begins.

**What "number of users" means:** This is the number of concurrent users at peak — the maximum number of simultaneous open conversations. It is not the number of messages per second. With think time set to 30–60 seconds, 10 concurrent users will generate approximately 10 ÷ 45 ≈ **0.2 requests per second**. To get 1 request per second, you need roughly 45 concurrent users. This is intentional — Microsoft's Copilot Studio performance guidance assumes human-paced conversations, not continuous bombardment.

**Recommended approach for first runs:**
1. Start with 1 user, confirm the bot responds correctly.
2. Step up to 5 users, watch for errors.
3. Step up to 10, 20, 50 users incrementally.

### 9.4 Stopping the test

Click **Stop** in the web interface. The tool will finish any in-flight requests and stop spawning new users.

---

## 10. Reading the Results

### 10.1 Real-time statistics table

The main table on the Locust dashboard shows one row per scenario (one per CSV file):

| Column | What it means |
|---|---|
| **Name** | The scenario name (derived from the CSV filename). |
| **# Requests** | Total number of bot replies received since the test started. |
| **# Fails** | Number of requests that timed out or returned an error. |
| **Median (ms)** | Half of all responses were faster than this. |
| **90%ile (ms)** | 90% of responses were faster than this. |
| **95%ile (ms)** | 95% of responses were faster than this. Compare this to your target (default 2000ms). |
| **99%ile (ms)** | 99% of responses were faster than this. |
| **Average (ms)** | Mean response time. Can be skewed by outliers; prefer the percentiles. |
| **Current RPS** | Requests per second right now. |

### 10.2 The charts tab

Click **Charts** to see response time and requests-per-second over time. A healthy run shows stable lines. Rising response times indicate the bot is becoming overwhelmed.

### 10.3 Interpreting results

**The bot passes the performance test if:**
- 95th-percentile response time stays below 2000ms (or your target)
- Error rate stays below 0.5% (or your target)

**Signs of trouble:**
- Response times climbing steadily → bot is under capacity pressure, needs more message capacity in Power Platform
- Error rate above 1% → bot may be throttling or returning errors; check Copilot Studio analytics
- "No bot reply received" errors → bot is taking too long to respond; increase the Response Timeout or reduce the number of concurrent users

---

## 11. Troubleshooting

### "This agent is currently unavailable. It has reached its usage limit."

This is a Copilot Studio capacity issue, not an authentication problem. The bot has exceeded the number of messages allocated to its Power Platform environment.

Fix: Go to Power Platform Admin Center → select your environment → Capacity → increase the message capacity assigned to this environment.

This is expected when running load tests with a trial or developer environment.

---

### "AADSTS650057: Invalid resource"

This means the token was requested for a resource (application) that has not been configured to accept delegated permissions.

Likely causes:
1. The `Bot Client ID (SSO)` field in the wizard is set to the wrong value (e.g., it matches the Client ID instead of the Agent App ID).
2. The `access_as_user` API permission was not added in Azure (see Section 4.5).
3. Admin consent was not granted after adding the permission.

---

### "AADSTS90009: Application is requesting a token for itself"

This means the Client ID and the Agent App ID are the same value. They must be different — one is the load test client, the other is the bot's resource app.

Fix: Re-run the wizard and enter the correct value for `Bot Client ID (SSO)` — it must be the client ID from the bot's existing app registration, not the load test app's client ID.

---

### "IntegratedAuthenticationNotSupportedInChannel"

The bot's authentication is set to "Authenticate with Microsoft" but you connected using a DirectLine Secret instead of through the Token Endpoint.

Fix: In the wizard, either:
- Clear the DirectLine Secret and use the Token Endpoint URL instead, or
- Fill in the `Bot Client ID (SSO)` field, which enables SSO token exchange over the DirectLine channel.

---

### "No valid token for [username]"

The cached token has expired and could not be refreshed silently. This happens if the refresh token has expired (typically after 90 days of inactivity) or if the account's password was changed.

Fix: Re-run the wizard and select the profile to re-authenticate: type the profile's number, then choose option `[2] Re-authenticate now`.

---

### Bot gives "sign in" prompt during the test

The SSO token exchange is not completing. The tool sent an OAuthCard but the bot did not accept the token.

Likely causes:
1. `AGENT_APP_ID` is blank or wrong — the tool cannot acquire a token for the bot's scope.
2. The token scope does not match. The tool uses `api://<AGENT_APP_ID>/access_as_user`. Verify this scope exists in the bot's app registration (Azure portal → the bot's app → Expose an API).
3. The Token Exchange URL in the bot's OAuth connection does not match the bot's app ID.

Run `python test_connection.py` for a detailed step-by-step diagnostic.

---

### `python test_connection.py` — what it checks

This is a standalone diagnostic script. Run it at any time to verify each step of the connection:

```
python test_connection.py
```

It checks, in order:
1. AAD token can be acquired for the first profile
2. DirectLine token can be fetched
3. A new conversation can be started
4. The WebSocket opens successfully
5. "hi" can be sent to the bot
6. The bot replies within 15 seconds
7. The reply is not a sign-in prompt (which would indicate the token exchange failed)

---

### Running `debug_oauth.py` — see the raw bot output

If you want to see exactly what the bot sends (including the OAuthCard details like connection name and token exchange URI), run:

```
python debug_oauth.py
```

This opens a conversation, sends "hi", and prints every activity the bot sends for 12 seconds without doing any token exchange. Useful for verifying bot configuration independently of the authentication code.

---

## File Reference

| File | Purpose |
|---|---|
| `run.py` | The main file. Contains everything: wizard, Locust user classes, DirectLine client, auth helpers. Run this to start the tool. |
| `test_connection.py` | Standalone connection diagnostic. Run this to verify setup without starting a full test. |
| `debug_oauth.py` | Raw activity inspector. Run this to see what the bot sends before any authentication processing. |
| `requirements.txt` | Python package dependencies. Install with `pip install -r requirements.txt`. |
| `.env.example` | Template showing all supported environment variables. Copy to `.env` if not using the wizard (advanced use). |
| `utterances/*.csv` | Test script files. One CSV per scenario. |
| `profiles/profiles.json` | List of test user accounts (created/managed by the wizard). Not committed to version control. |
| `profiles/.tokens/` | Encrypted cached tokens. One file per user account. Not committed to version control. |
| `profiles/profiles.example.json` | Example of the profiles.json format, for reference. |
