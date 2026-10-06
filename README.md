# CS-Knife

**An AI agent that automates customer-service ticket handling — from first player message to resolved ticket.**

CS-Knife watches a browser-based support workbench, understands each incoming ticket against a knowledge base, drafts (or sends) compliant replies via DeepSeek, and lets one agent supervise everything from a desktop HUD or an iPhone. Built to take over the repetitive bulk of ticket work while keeping a human in control of every word that reaches a customer.

## 中文简介

CS-Knife 是一个 AI 客服工单自动化 Agent：实时抓取浏览器客服工作台的工单，对照知识库（Excel 规章库）理解问题，用 DeepSeek 生成合规回复，支持手动 / 半自动 / 全自动三档模式。电脑端有桌面悬浮窗 + 全局热键一键出话术，手机端（iPhone H5）可远程看单、改状态、代回复、一键关单。设计目标是把重复性工单处理自动化，同时"人"始终掌握最终发送权。

---

## The problem it solves

Support agents answer the same questions dozens of times a day: top-ups not arriving, login failures, event-rule confusion. Each ticket means reading history, finding the right policy, writing a careful reply — and at 2 AM, nobody is there at all. Hiring more agents is expensive; generic chatbots answer wrong and nobody is accountable.

CS-Knife takes a different route: **it doesn't replace the agent, it gives one agent leverage** — AI drafts from your own rulebook, you confirm with one keystroke, and overnight the AFK mode holds the fort with guardrails.

## What it actually does

**Ticket understanding**
- Captures live tickets from the workbench page via a Tampermonkey probe (`probe.js`) — no changes to the official system, no API access required
- Matches each ticket against a knowledge base (`rules.xlsx`, 13 sheets / 800+ entries: scripts, FAQs, known bugs, closing categories, announcements); auto-detects 6 table layouts, retrieval-injects only the relevant entries to control token cost
- Classifies intent: greeting / needs-info / normal / waiting / timeout-close

**Reply generation with guardrails**
- DeepSeek drafts replies grounded in the matched rules — never free-styled
- A speech safety gate rewrites or blocks anything risky before it can reach a customer (internal jargon, compensation promises, guarantees, "bug"/"exploit" wording…); anything the rulebook doesn't cover is escalated, never guessed
- Human-like pacing: no instant replies — waits 1–3 random minutes after the player finishes typing, so it doesn't trip bot detection or interrupt the player

**Three operating modes**
| Mode | Behavior |
|------|----------|
| ✋ Manual | Notifies only — AI never drafts |
| 🤖 Semi-auto (default) | AI drafts into the reply box, human confirms and sends |
| 🚀 AFK | AI replies directly; unresolved tickets get pinned + pushed to your phone |

**Desktop HUD + hotkeys** (`semi_runner.pyw`)
- Always-on-top overlay: connection lights (relay / probe / phone), live ticket summary
- F9 — AI analyzes the current ticket and fills the reply box (no text selection needed)
- F7/F8 — one-key ticket digest; F10 — polish a rough draft into official tone; F6 — grab media links

**iPhone remote console** (mobile H5 via `bridge_server.py`)
- Live session list, new-message chime + vibration, unread badges
- Switch agent status, hang up / resume tickets, send on behalf, AI reply-and-close with one tap
- Bark push alerts: dropped connection (siren), needs-human (triple beep), new message

**Knowledge base that never goes stale** (`rules_sync.py`)
- Three no-API sync paths: watch-folder drop, direct xlsx link, or manual overwrite with hot-reload — backups + corruption rejection built in

**Ops-grade self-awareness**
- `http://127.0.0.1:8765/diag` — one-page health of relay / probe / phone / knowledge base
- Old-script auto-detection, per-ticket isolation fingerprints, full audit trail of AI-suggested closings

## How it works

```
Player message
     │  probe.js (Tampermonkey) captures DOM from the workbench page
     ▼
bridge_server.py ── WebSocket relay + iPhone H5 console (port 8765)
     │  AI call
     ▼
agent_core.py ── KB retrieval → DeepSeek draft → speech safety gate
     │
     ├─► semi-auto: draft filled into reply box, human presses send
     └─► AFK: sent automatically · unknown cases escalated to phone
```

## Why it's not a toy project

- **15 automated test suites** — probe smoke tests, H5 security/XSS tests, agent-core regression, rules-sync, HUD layout, diagnostics end-to-end
- **Public-safe by design** — the repo ships with generic brand placeholders only; real company/game/player-honorific names live in a local `config.json` (git-ignored) and are substituted at runtime. The code can be public while a deployment stays private.
- **Defensive by default** — every AI output passes the safety gate; semi-auto mode physically cannot auto-send (code-level interlock); closing a ticket with the wrong category is refused rather than guessed.
- **Built from real ops pain** — offline-status guarding, multi-ticket isolation, wake-up re-sync for iOS backgrounding, failure reasons that name the real button on the page.

## Honest scope

- Runs on **Windows 10/11** + Chrome/Edge + Tampermonkey; iPhone on the same LAN for the mobile console
- Needs a **DeepSeek API key** and a knowledge base (Excel) — the quality of replies is bounded by the quality of your rulebook
- The page probe needs **per-site DOM calibration** for a new workbench (ticket-ID picker is documented and isolated in one function)
- This is a **force multiplier for human agents**, not a lights-out replacement: someone should own the escalation queue

## Quickstart

```bash
pip install -r requirements.txt
cp config.json.template config.json   # then fill in deepseek_api_key, bark_key, brand names
python launcher.py                     # starts relay + desktop HUD
# Tampermonkey → new script → paste probe.js → enable
# iPhone Safari → http://<your-pc-ip>:8765
```

Full operator manual (hotkeys, mobile console, rules sync, troubleshooting) lives in the repo's existing docs.

## Project structure

```
probe.js            Tampermonkey probe — DOM capture, action execution
bridge_server.py    WebSocket relay + iPhone H5 console + /diag
agent_core.py       KB retrieval, DeepSeek decisions, speech safety gate
semi_runner.pyw     Desktop HUD overlay + global hotkeys
rules_sync.py       Knowledge-base auto-sync (no API needed)
distill_rules.py    Offline: distill human reply samples into rules
qc_probe.js         QC-page collector for historical session corpus
*_test.py / *_test.js   15 automated test suites
```

## Roadmap

- [ ] Per-site probe calibration profiles (one-click switching between workbenches)
- [ ] Reply quality scoring + weekly auto-report
- [ ] More LLM backends behind one interface (DeepSeek default)

---

*Built by Xavier Cai — ex-Ubisoft game QA, now building automation for support teams.
Available for freelance: support-automation builds, QA/LQA, AI evals. → k_981@outlook.com*
