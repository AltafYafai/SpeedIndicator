#!/usr/bin/env python3
"""Telegram Bot API integration for GitHub Actions.

Sends a build message to Telegram, edits it live as build stages complete,
uploads a built APK, and marks the build as succeeded or failed.

All secrets are optional: when TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID are
unset (e.g. forks or pull requests), the script prints a notice and exits 0
so the workflow stays green.

Required secrets:
  TELEGRAM_BOT_TOKEN  - bot token from @BotFather
  TELEGRAM_CHAT_ID    - target chat id (group, channel, or user)

Environment:
  GITHUB_*                  - provided by GitHub Actions (repo, ref, sha, run url...)
  GITHUB_WORKSPACE          - used to persist the message id between steps
  TELEGRAM_MESSAGE_FILE     - override the state file path (testing)
  TELEGRAM_API_BASE         - override https://api.telegram.org/bot<token> (testing)

Usage:
  telegram_bot.py start                        - post the "build started" message
  telegram_bot.py stage done   <key> <text...> - mark a stage finished
  telegram_bot.py stage active <key> <text...> - mark a stage in progress
  telegram_bot.py apk <path>                   - upload APK and mark success
  telegram_bot.py failure [text...]            - mark the current build failed
  telegram_bot.py result <job-status>          - success/failure from ${{ job.status }}
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from html import escape
from pathlib import Path


BOT_API_BASE = "https://api.telegram.org"


def log(message: str) -> None:
    print(f"[telegram] {message}", flush=True)


def env_or_default(name: str, default: str) -> str:
    return os.environ.get(name, default)


class TelegramClient:
    def __init__(self) -> None:
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    @property
    def available(self) -> bool:
        return bool(self.token) and bool(self.chat_id)

    @property
    def base_url(self) -> str:
        base = os.environ.get("TELEGRAM_API_BASE", "").strip()
        if base:
            return base.rstrip("/")
        return f"{BOT_API_BASE}/bot{self.token}"

    def call(self, method: str, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} failed: HTTP {exc.code}: {detail}") from exc
        except Exception as exc:
            raise RuntimeError(f"{method} failed: {exc}") from exc

        result = json.loads(body)
        if not result.get("ok"):
            raise RuntimeError(f"{method} failed: {result}")
        return result.get("result", {})

    def send_message(self, text: str) -> int:
        result = self.call(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        return int(result["message_id"])

    def edit_message(self, message_id: int, text: str) -> None:
        try:
            self.call(
                "editMessageText",
                {
                    "chat_id": self.chat_id,
                    "message_id": message_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
        except RuntimeError as exc:
            # e.g. message too old or content unchanged - don't fail the build
            if "message is not modified" in str(exc):
                return
            log(f"warning: could not edit message {message_id}: {exc}")

    def send_document(self, file_path: Path, caption: str) -> None:
        command = [
            "curl",
            "-sS",
            "-F",
            f"chat_id={self.chat_id}",
            "-F",
            f"caption={caption}",
            "-F",
            "parse_mode=HTML",
            "-F",
            f"document=@{file_path}",
            "-w",
            "\n%{http_code}",
            f"{self.base_url}/sendDocument",
        ]
        output = subprocess.run(command, capture_output=True, text=True, check=False)
        raw = output.stdout.strip()
        body, _, code = raw.rpartition("\n")
        try:
            result = json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(f"sendDocument returned HTTP {code} with body: {raw}")
        if not result.get("ok"):
            raise RuntimeError(f"sendDocument failed: {result}")


def state_file() -> Path:
    override = os.environ.get("TELEGRAM_MESSAGE_FILE", "").strip()
    if override:
        return Path(override)
    workspace = Path(os.environ.get("GITHUB_WORKSPACE", "."))
    return workspace / ".telegram-message"


def run_url() -> str:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    return f"{server}/{repository}/actions/runs/{run_id}"


def run_number() -> str:
    return os.environ.get("GITHUB_RUN_NUMBER", "?")


def build_header(title: str) -> str:
    repository = escape(os.environ.get("GITHUB_REPOSITORY", "unknown/repo"))
    ref = escape(os.environ.get("GITHUB_REF_NAME", os.environ.get("GITHUB_REF", "main")))
    sha = escape(os.environ.get("GITHUB_SHA", "")[:7])
    actor = escape(os.environ.get("GITHUB_ACTOR", "unknown"))
    event = escape(os.environ.get("GITHUB_EVENT_NAME", "manual"))
    url = run_url()
    number = escape(run_number())
    return (
        f"{title}\n"
        f"<b>Repo:</b> {repository}\n"
        f"<b>Branch:</b> {ref} · <code>{sha}</code>\n"
        f"<b>Trigger:</b> {event} by <b>{actor}</b>\n"
        f"<b>Run:</b> <a href=\"{url}\">#{number}</a>\n"
    )


STAGE_ICONS = {"done": "✅", "active": "🔄", "failed": "❌", "pending": "⏳"}


def render(state: dict, title: str) -> str:
    lines = [build_header(title), "<b>Progress</b>"]
    for stage in state.get("stages", []):
        icon = STAGE_ICONS.get(stage["status"], "⏳")
        lines.append(f"{icon} {escape(stage['text'])}")
    elapsed = time.time() - state.get("started_at", time.time())
    if elapsed >= 60:
        minutes, seconds = divmod(int(elapsed), 60)
        lines.append(f"<i>elapsed {minutes}m {seconds}s</i>")
    else:
        lines.append(f"<i>elapsed {int(elapsed)}s</i>")
    return "\n".join(lines)


def load_state() -> dict | None:
    path = state_file()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log(f"state file {path} is corrupt; starting fresh")
        return None


def save_state(state: dict) -> None:
    state_file().write_text(json.dumps(state), encoding="utf-8")


def fresh_state(client: TelegramClient) -> dict:
    message_id = client.send_message(build_header("🚀 <b>Build started</b>"))
    return {
        "message_id": message_id,
        "stages": [],
        "started_at": time.time(),
    }


def guard(client: TelegramClient) -> bool:
    if client.available:
        return True
    log("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set; skipping Telegram notification.")
    return False


def upsert_stage(state: dict, key: str, status: str, text: str) -> None:
    for stage in state["stages"]:
        if stage["key"] == key:
            stage["status"] = status
            stage["text"] = text
            return
    state["stages"].append({"key": key, "status": status, "text": text})


def update_message(client: TelegramClient, state: dict, title: str) -> None:
    client.edit_message(state["message_id"], render(state, title))
    save_state(state)


def mark_failure(client: TelegramClient, state: dict | None, detail: str) -> None:
    if state is not None:
        for stage in state["stages"]:
            if stage["status"] == "active":
                stage["status"] = "failed"
                break
        text = render(state, "❌ <b>Build failed</b>")
        if detail:
            text += f"\n{escape(detail)}"
        client.edit_message(state["message_id"], text)
        return
    client.send_message(f"{build_header('❌ <b>Build failed</b>')}\n{escape(detail)}")


def need_state(client: TelegramClient) -> tuple[TelegramClient, dict] | None:
    client_ = client
    state = load_state()
    if state is None:
        state = fresh_state(client_)
    return client_, state


def cmd_start(client: TelegramClient) -> None:
    state = fresh_state(client)
    save_state(state)
    log(f"posted build message #{state['message_id']}")


def cmd_stage(client: TelegramClient, status: str, key: str, text: str) -> None:
    loaded = need_state(client)
    if loaded is None:
        return
    _, state = loaded
    upsert_stage(state, key, status, text)
    title = "🚀 <b>Build in progress</b>"
    update_message(client, state, title)


def cmd_apk(client: TelegramClient, apk_path: Path) -> None:
    if not apk_path.is_file():
        raise RuntimeError(f"APK is missing: {apk_path}")
    size_mb = apk_path.stat().st_size / (1024 * 1024)
    if size_mb > 48.0:
        log(f"warning: {size_mb:.1f} MB exceeds the Bot API 50 MB upload ceiling")

    loaded = need_state(client)
    if loaded is None:
        return
    _, state = loaded

    caption = (
        f"📦 <b>SpeedIndicator debug APK</b>\n"
        f"<b>Branch:</b> {escape(os.environ.get('GITHUB_REF_NAME', '?'))} · "
        f"<code>{escape(os.environ.get('GITHUB_SHA', '')[:7])}</code>\n"
        f"<b>Size:</b> {size_mb:.1f} MB\n"
        f"<b>Run:</b> <a href=\"{run_url()}\">#{escape(run_number())}</a>"
    )
    try:
        client.send_document(apk_path, caption)
    except RuntimeError:
        upsert_stage(state, "upload", "failed", "APK upload to Telegram failed")
        save_state(state)
        raise

    upsert_stage(state, "upload", "done", f"APK uploaded to Telegram ({size_mb:.1f} MB)")
    update_message(client, state, "✅ <b>Build finished</b>")


def cmd_failure(client: TelegramClient, detail: str) -> None:
    state = load_state()
    mark_failure(client, state, detail)


def cmd_result(client: TelegramClient, job_status: str) -> None:
    if job_status.lower() == "success":
        state = load_state()
        if state is not None:
            update_message(client, state, "✅ <b>Build finished</b>")
        else:
            client.send_message(build_header("✅ <b>Build finished</b>"))
    else:
        cmd_failure(client, f"Job status: {job_status or 'unknown'}")


def main() -> int:
    client = TelegramClient()
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)

    command = args[0]
    if not guard(client):
        return 0

    if command == "start":
        cmd_start(client)
        return 0
    if command == "stage" and len(args) >= 4 and args[1] in ("done", "active"):
        status, key, text = args[1], args[2], " ".join(args[3:])
        cmd_stage(client, status, key, text)
        return 0
    if command == "apk" and len(args) == 2:
        cmd_apk(client, Path(args[1]))
        return 0
    if command == "failure":
        cmd_failure(client, " ".join(args[1:]))
        return 0
    if command == "result" and len(args) == 2:
        cmd_result(client, args[1])
        return 0

    raise SystemExit(f"Unknown command: {' '.join(args)}\n{__doc__}")


if __name__ == "__main__":
    sys.exit(main())
