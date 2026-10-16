"""Exercise the page inside AstrBot's sandbox with the real bridge SDK."""

import json
import mimetypes
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import pytest

playwright = pytest.importorskip("playwright.sync_api")
PLUGIN = Path(__file__).resolve().parents[1]
TASK_ID = "a" * 32
SAMPLE_TIME = datetime(2026, 10, 13, 8, tzinfo=timezone(timedelta(hours=8))).timestamp()

HARNESS = """<!doctype html><html><head><style>body{margin:0}iframe{width:100%;height:100vh;border:0;display:block}</style></head><body>
<iframe id="plugin" src="/page/index.html" sandbox="allow-scripts allow-forms allow-downloads"></iframe>
<script>
window.addEventListener('message',async event=>{
 const m=event.data;if(!m||m.channel!=='astrbot-plugin-page')return;
 const reply=body=>event.source.postMessage({channel:'astrbot-plugin-page',...body},'*');
 if(m.kind==='ready'){reply({kind:'context',context:{isDark:false,locale:'zh-CN',pluginName:'astrbot_plugin_rclone',pageName:'sync'}});return;}
 if(m.kind!=='request')return;
 try{
  let data;
  if(m.action==='files:download'){data={filename:m.filename};}
  else{const r=await fetch('/api/'+m.endpoint,{method:m.action==='api:post'?'POST':'GET',headers:{'Content-Type':'application/json'},body:m.action==='api:post'?JSON.stringify(m.body):undefined});data=await r.json();if(data.status==='error')throw new Error(data.message);}
  reply({kind:'response',requestId:m.requestId,ok:true,data});
 }catch(e){reply({kind:'response',requestId:m.requestId,ok:false,error:e.message});}
});
</script></body></html>"""


@pytest.fixture
def browser_page():
    state = {
        "csrf": "test-csrf",
        "configuration": {"active": False},
        "installation": {"status": "idle", "received": 0, "total": None, "error": ""},
        "connection": {
            "binary": "rclone",
            "remote": "onedrive",
            "folder": "AstrBotSync",
        },
        "config_path": "/AstrBot/data/plugin_data/astrbot_plugin_rclone/rclone.conf",
        "commands": {
            "setup": "rclone config --config /private/rclone.conf",
            "reconnect": "rclone config reconnect onedrive: --config /private/rclone.conf",
        },
        "root": "/AstrBot",
        "cloud": {
            "connected": False,
            "error": "",
            "version": "",
        },
        "examples": {
            "plugins": "/AstrBot/data/plugin_data",
            "backups": "/AstrBot/data/backups",
        },
        "tasks": [],
        "runs": [],
        "busy": False,
    }
    posts = []
    remotes = []
    pending = {}
    providers = [
        {
            "Name": "s3",
            "Description": "S3 compatible storage",
            "Options": [
                {
                    "Name": "provider",
                    "Type": "string",
                    "Exclusive": True,
                    "Examples": [
                        {"Value": "Other", "Help": "S3 compatible"},
                        {"Value": "AWS", "Help": "Amazon"},
                    ],
                },
                {"Name": "access_key_id", "Type": "string", "Sensitive": True},
                {"Name": "secret_access_key", "Type": "string", "Sensitive": True},
                {"Name": "endpoint", "Type": "string", "Provider": "Other"},
                {"Name": "region", "Type": "string", "Advanced": True},
            ],
        },
        {"Name": "onedrive", "Description": "Microsoft OneDrive", "Options": []},
    ]

    def route_handler(route):
        path = urlparse(route.request.url).path
        if path == "/":
            return route.fulfill(body=HARNESS, content_type="text/html")
        if path == "/bridge.js":
            sdk = (
                PLUGIN.parent
                / "AstrBot"
                / "astrbot"
                / "dashboard"
                / "plugin_page_bridge.js"
            )
            return route.fulfill(
                body=sdk.read_text(encoding="utf-8"), content_type="text/javascript"
            )
        if path.startswith("/page/"):
            file = PLUGIN / "pages" / "sync" / path.removeprefix("/page/")
            content = file.read_text(encoding="utf-8")
            if file.name == "index.html":
                content = content.replace(
                    "<head>", '<head><script src="/bridge.js"></script>'
                )
            return route.fulfill(
                body=content,
                content_type=mimetypes.guess_type(file.name)[0] or "text/plain",
            )
        action = path.removeprefix("/api/")
        payload = (
            route.request.post_data_json if route.request.method == "POST" else None
        )
        if payload:
            posts.append((action, payload))
            assert payload.pop("csrf") == "test-csrf"
        result = {"saved": True}
        if action == "providers":
            result = {"providers": state.get("provider_catalog", providers)}
        elif action == "remotes":
            result = {"remotes": remotes}
        elif action == "config-fields":
            result = {
                "provider": next(
                    p
                    for p in state.get("provider_catalog", providers)
                    if p["Name"] == payload["backend"]
                ),
                "values": {},
                "configured": [],
            }
        elif action in {"config-begin", "config-reconnect"}:
            if action == "config-reconnect":
                payload = {"name": state["connection"]["remote"], "backend": "onedrive"}
            pending.update(payload)
            state["configuration"] = {
                "active": True,
                "mode": "form",
                "expires_at": SAMPLE_TIME,
                "session_id": "test-session",
            }
            state["cloud"]["connected"] = False
            if payload["backend"] == "onedrive":
                result = {
                    "session_id": "test-session",
                    "complete": False,
                    "option": state.get(
                        "config_question",
                        {
                            "Name": "config_token",
                            "Type": "string",
                            "IsPassword": True,
                            "Required": True,
                            "Help": 'Run rclone authorize "onedrive", then paste the result.',
                        },
                    ),
                }
            else:
                result = {"session_id": "test-session", "complete": True}
        elif action == "config-answer":
            result = {"session_id": "test-session", "complete": True}
        elif action == "config-finish":
            if payload["save"] and pending:
                remotes.append({"name": pending["name"], "type": pending["backend"]})
            state["configuration"] = {"active": False}
            pending.clear()
        elif action == "gui-start":
            state["configuration"] = {
                "active": True,
                "mode": "gui",
                "expires_at": SAMPLE_TIME,
                "session_id": "test-gui",
            }
            result = {
                "session_id": "test-gui",
                "login_url": "http://127.0.0.1:5580/login?pass=test-gui-pass",
            }
        elif action == "state":
            result = state
        elif action == "install-rclone":
            state["installation"] = {
                "status": "downloading",
                "version": "v1.75.2",
                "received": 1024,
                "total": 2048,
                "error": "",
            }
            state["cloud"]["connected"] = False
            state["busy"] = True
        elif action == "connection":
            state["connection"] = payload
            state["cloud"]["connected"] = False
        elif action == "connect":
            state["cloud"] = {
                "connected": True,
                "error": "",
                "version": "rclone v1.75.2",
            }
        elif action == "disconnect":
            state["cloud"]["connected"] = False
        elif action == "preview":
            result = {
                "counts": {
                    "add": 2,
                    "update": 1,
                    "delete": 1 if "mirror" in payload["mode"] else 0,
                    "unchanged": 20,
                },
                "changes": [
                    {"action": "add", "path": "new.txt"},
                    {"action": "delete", "path": "old.txt"},
                ],
                "truncated": False,
            }
        elif action == "save-task":
            task = {**payload, "id": TASK_ID, "next_run": SAMPLE_TIME}
            state["tasks"] = [task]
        elif action == "delete-task":
            state["tasks"] = []
        elif action == "run":
            task = state["tasks"][0]
            state["runs"] = [
                {
                    "id": "b" * 32,
                    "task_id": task["id"],
                    "task_name": task["name"],
                    "mode": task["mode"],
                    "local_path": task["local_path"],
                    "remote_path": "onedrive:AstrBotSync/" + task["remote_path"],
                    "status": "syncing",
                    "created_at": SAMPLE_TIME,
                    "scheduled": False,
                    "finished_at": None,
                    "error": "",
                    "warning": "",
                    "progress": {
                        "bytes": 1024,
                        "totalBytes": 2048,
                        "transfers": 1,
                        "totalTransfers": 2,
                        "deletes": 1,
                        "speed": 512,
                    },
                }
            ]
            state["busy"] = True
        elif action == "cancel":
            state["runs"][0]["status"] = "cancelled"
            state["runs"][0]["finished_at"] = SAMPLE_TIME
            state["busy"] = False
        return route.fulfill(body=json.dumps(result), content_type="application/json")

    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("http://backup.test/**", route_handler)
        page.goto("http://backup.test/")
        frame = page.frame_locator("#plugin")
        frame.locator("#new-task").wait_for(state="visible")
        playwright.expect(frame.locator("#new-task")).to_be_enabled()
        yield page, frame, state, posts
        assert not errors
        browser.close()


def test_page_creates_previews_edits_and_removes_tasks(browser_page):
    page, frame, state, posts = browser_page
    state["cloud"]["connected"] = True
    frame.locator("#refresh").click()
    frame.locator("#new-task").click()
    frame.locator("#task-name").fill("插件数据同步")
    frame.locator("#task-remote").fill("plugin-data")
    frame.locator("#example-plugins").click()
    playwright.expect(frame.locator("#task-local")).to_have_value(
        "/AstrBot/data/plugin_data"
    )
    frame.locator("#task-interval").fill("1440")
    frame.locator("#task-exclude").fill("*.tmp\ncache/**")
    frame.locator("#preview").click()
    playwright.expect(frame.locator("#preview-result")).to_contain_text("新增 2")
    frame.locator("#save-task").click()
    playwright.expect(frame.locator("#task-dialog")).not_to_be_visible()
    playwright.expect(frame.locator("#task-list")).to_contain_text("插件数据同步")
    assert state["tasks"][0]["interval_minutes"] == 1440
    assert state["tasks"][0]["exclude"] == ["*.tmp", "cache/**"]
    frame.locator("#task-list").get_by_role("button", name="编辑", exact=True).click()
    playwright.expect(frame.locator("#task-interval")).to_have_value("1440")
    frame.locator("#close-task").click()
    frame.get_by_role("button", name="移除", exact=True).click()
    frame.locator("#confirm-action").click()
    playwright.expect(frame.locator("#task-list")).to_contain_text(
        "选择方向，创建同步任务"
    )
    assert not state["tasks"]


def test_rclone_connection_settings_and_setup_work_in_sandbox(browser_page):
    page, frame, state, posts = browser_page
    frame.locator("#rclone-remote").fill("my_onedrive")
    frame.locator("#remote-folder").fill("My Backups")
    frame.locator("#save-connection").click()
    playwright.expect(frame.locator("#message")).to_contain_text("连接设置已保存")
    playwright.expect(frame.locator("#connect")).to_be_enabled()
    frame.locator("#connect").click()
    playwright.expect(frame.locator("#connection-label")).to_have_text("已连接")
    assert state["connection"]["remote"] == "my_onedrive"
    assert state["connection"]["folder"] == "My Backups"
    frame.locator("#open-guide").click()
    frame.locator('[data-guide-step="storage"]').click()
    playwright.expect(frame.locator("#setup-command")).to_have_value(
        state["commands"]["setup"]
    )
    frame.locator("#close-guide").click()
    frame.locator("#disconnect").click()
    frame.locator("#confirm-action").click()
    playwright.expect(frame.locator("#connection-label")).to_have_text("未连接")


def test_guide_buttons_reuse_forms_and_support_reauthorization_cancel_and_save(
    browser_page,
):
    page, frame, state, posts = browser_page
    frame.locator("#open-guide").click()
    frame.locator('[data-guide-step="storage"]').click()
    frame.locator("#run-setup").click()
    playwright.expect(frame.locator("#guide-dialog")).not_to_be_visible()
    playwright.expect(frame.locator("#config-title")).to_have_text("添加存储连接")
    frame.locator("#close-config").click()
    frame.locator("#rclone-remote").fill("unsaved")
    frame.locator("#open-guide").click()
    frame.locator('[data-guide-step="authorization"]').click()
    playwright.expect(frame.locator("#guide-settings-notice")).to_be_visible()
    playwright.expect(frame.locator("#run-reconnect")).to_be_disabled()
    frame.locator("#close-guide").click()
    frame.locator("#save-connection").click()
    frame.locator("#open-guide").click()
    playwright.expect(frame.locator("#guide-settings-notice")).not_to_be_visible()
    frame.locator("#run-reconnect").click()
    playwright.expect(frame.locator("#config-title")).to_have_text("重新授权 · unsaved")
    playwright.expect(frame.locator("#config-question")).to_contain_text(
        "rclone authorize"
    )
    playwright.expect(frame.locator("#run-setup")).to_be_disabled()
    frame.locator("#close-config").click()
    playwright.expect(frame.locator("#config-dialog")).not_to_be_visible()
    assert any(
        action == "config-finish" and body["save"] is False for action, body in posts
    )
    frame.locator("#open-guide").click()
    frame.locator("#run-reconnect").click()
    frame.locator('#config-question [data-option="config_token"]').fill("new-token")
    frame.locator("#submit-config").click()
    frame.locator("#submit-config").click()
    playwright.expect(frame.locator("#config-dialog")).not_to_be_visible()
    playwright.expect(frame.locator("#message")).to_contain_text("授权配置已保存")
    playwright.expect(frame.locator("#connect")).to_be_enabled()
    assert any(action == "config-reconnect" and body == {} for action, body in posts)


def test_installer_displays_progress_success_and_failures_in_guide(browser_page):
    page, frame, state, posts = browser_page
    frame.locator("#open-guide").click()
    frame.locator("#install-rclone").click()
    playwright.expect(frame.locator("#install-status")).to_contain_text(
        "1.0 KiB / 2.0 KiB"
    )
    playwright.expect(frame.locator("#run-setup")).to_be_disabled()
    assert any(action == "install-rclone" and body == {} for action, body in posts)
    state["busy"] = False
    state["installation"].update(status="failed", error="校验失败，请重试。")
    frame.locator("#close-guide").click()
    frame.locator("#refresh").click()
    frame.locator("#open-guide").click()
    playwright.expect(frame.locator("#install-status")).to_contain_text("校验失败")
    playwright.expect(frame.locator("#install-rclone")).to_be_enabled()
    state["installation"].update(status="completed")
    state["connection"]["binary"] = "/private/bin/rclone"
    frame.locator("#close-guide").click()
    frame.locator("#refresh").click()
    frame.locator("#open-guide").click()
    playwright.expect(frame.locator("#install-status")).to_contain_text(
        "程序路径已保存"
    )
    playwright.expect(frame.locator("#rclone-binary")).to_have_value(
        "/private/bin/rclone"
    )


@pytest.mark.parametrize("width,dark", [(1280, False), (390, False), (1280, True)])
def test_layout_and_theme_have_no_horizontal_overflow(browser_page, width, dark):
    page, frame, state, posts = browser_page
    state["tasks"] = [
        {
            "id": TASK_ID,
            "name": "插件数据定时同步",
            "mode": "upload_mirror",
            "local_path": "/AstrBot/data/plugin_data/example",
            "remote_path": "plugin-data",
            "exclude": ["*.tmp"],
            "interval_minutes": 1440,
            "enabled": True,
            "next_run": SAMPLE_TIME,
        }
    ]
    state["cloud"]["connected"] = True
    page.set_viewport_size({"width": width, "height": 1000})
    frame.locator("#refresh").click()
    playwright.expect(frame.locator("#task-list")).to_contain_text("插件数据定时同步")
    page.evaluate(
        "dark => document.querySelector('iframe').contentWindow.postMessage({channel:'astrbot-plugin-page',kind:'context',context:{isDark:dark}},'*')",
        dark,
    )
    if dark:
        playwright.expect(frame.locator("html")).to_have_attribute("data-theme", "dark")
    assert frame.locator("body").evaluate(
        "element => element.scrollWidth <= window.innerWidth"
    )
    output = PLUGIN / "tests" / "output"
    output.mkdir(exist_ok=True)
    name = "dark" if dark else "mobile" if width < 500 else "desktop"
    page.screenshot(path=str(output / f"{name}.png"))
    frame.locator("#new-task").click()
    assert frame.locator("body").evaluate(
        "element => element.scrollWidth <= window.innerWidth"
    )
    if width == 1280 and not dark:
        page.screenshot(path=str(output / "editor.png"))
    frame.locator("#close-task").click()
    frame.locator("#open-guide").click()
    for step in ("install", "storage", "connection", "tasks", "authorization"):
        frame.locator(f'[data-guide-step="{step}"]').click()
        assert frame.locator("#guide-dialog").evaluate(
            "e => e.scrollWidth <= e.clientWidth"
        )
    frame.locator('[data-guide-step="storage"]').click()
    assert frame.locator("body").evaluate(
        "element => element.scrollWidth <= window.innerWidth"
    )
    page.screenshot(path=str(output / f"guide-{name}.png"), full_page=True)


def test_guide_navigation_opens_existing_controls_and_returns_to_settings(browser_page):
    page, frame, state, posts = browser_page
    frame.locator("#open-guide").click()
    playwright.expect(frame.locator("#guide-install")).to_be_visible()
    playwright.expect(frame.locator("#guide-previous")).to_be_disabled()
    frame.locator("#guide-next").click()
    playwright.expect(frame.locator("#guide-storage")).to_be_visible()
    assert not posts
    frame.locator("#guide-previous").click()
    playwright.expect(frame.locator("#guide-install")).to_be_visible()
    frame.locator("#guide-next").click()
    frame.locator("#guide-open-gui").click()
    playwright.expect(frame.locator("#guide-dialog")).not_to_be_visible()
    playwright.expect(frame.locator("#gui-dialog")).to_be_visible()
    frame.locator("#close-gui").click()
    frame.locator("#open-guide").click()
    playwright.expect(frame.locator("#guide-storage")).to_be_visible()
    frame.locator('[data-guide-step="connection"]').click()
    frame.locator('[data-guide-focus="rclone-remote"]').click()
    playwright.expect(frame.locator("#guide-dialog")).not_to_be_visible()
    playwright.expect(frame.locator("#rclone-remote")).to_be_focused()
    frame.locator("#open-guide").click()
    frame.locator('[data-guide-step="tasks"]').click()
    frame.locator("#guide-new-task").click()
    playwright.expect(frame.locator("#guide-dialog")).not_to_be_visible()
    playwright.expect(frame.locator("#task-dialog")).to_be_visible()
    frame.locator("#close-task").click()
    frame.locator("#open-guide").click()
    frame.locator("#guide-next").click()
    playwright.expect(frame.locator("#guide-authorization")).to_be_visible()
    playwright.expect(frame.locator("#guide-next")).to_have_text("完成，返回页面")
    frame.locator("#guide-next").click()
    playwright.expect(frame.locator("#guide-dialog")).not_to_be_visible()
    frame.locator("#open-guide").click()
    frame.locator("#close-guide").press("Escape")
    playwright.expect(frame.locator("#guide-dialog")).not_to_be_visible()


@pytest.mark.parametrize("width,dark", [(1280, False), (390, False), (1280, True)])
def test_dynamic_s3_form_saves_connection_in_sandbox(browser_page, width, dark):
    page, frame, state, posts = browser_page
    page.set_viewport_size({"width": width, "height": 1000})
    if dark:
        page.evaluate(
            "document.querySelector('iframe').contentWindow.postMessage({channel:'astrbot-plugin-page',kind:'context',context:{isDark:true}},'*')"
        )
    frame.locator("#create-remote").click()
    frame.locator("#config-name").fill("s3_backup")
    frame.locator("#config-backend").select_option("s3")
    frame.locator('[data-option="provider"]').select_option("Other")
    frame.locator('[data-option="access_key_id"]').fill("demo-key")
    frame.locator('[data-option="secret_access_key"]').fill("demo-secret")
    frame.locator('[data-option="endpoint"]').fill("https://s3.example.com")
    assert (
        frame.locator('[data-option="secret_access_key"]').get_attribute("type")
        == "password"
    )
    assert frame.locator("body").evaluate("e => e.scrollWidth <= window.innerWidth")
    output = PLUGIN / "tests" / "output"
    output.mkdir(exist_ok=True)
    page.screenshot(
        path=str(output / f"config-{width}-{'dark' if dark else 'light'}.png")
    )
    frame.locator("#submit-config").click()
    playwright.expect(frame.locator("#submit-config")).to_have_text("保存连接")
    playwright.expect(frame.locator("#connect")).to_be_disabled()
    frame.locator("#submit-config").click()
    playwright.expect(frame.locator("#config-dialog")).not_to_be_visible()
    playwright.expect(frame.locator("#rclone-remote")).to_have_value("s3_backup")
    saved = next(body for action, body in posts if action == "config-begin")
    assert saved["parameters"]["provider"] == "Other"
    assert saved["parameters"]["endpoint"] == "https://s3.example.com"
    frame.locator("#remote-folder").fill("mybucket/AstrBotSync")
    frame.locator("#save-connection").click()
    playwright.expect(frame.locator("#connect")).to_be_enabled()


@pytest.mark.parametrize(
    "default,selection,answer",
    [
        ("drive-archive", None, "drive-archive"),
        ("drive-archive", "drive-main", "drive-main"),
        ("drive-archive", "custom", "custom-drive"),
        ("saved-drive", None, "saved-drive"),
    ],
)
def test_drive_question_shows_all_choices_and_preserves_free_input(
    browser_page, default, selection, answer
):
    page, frame, state, posts = browser_page
    page.set_viewport_size({"width": 390, "height": 1000})
    state["config_question"] = {
        "Name": "config_driveid",
        "Type": "string",
        "Required": True,
        "Exclusive": False,
        "Default": default,
        "Examples": [
            {"Value": "drive-archive", "Help": "Archive (personal)"},
            {"Value": "drive-main", "Help": "OneDrive (personal)"},
            {"Value": "drive-shared", "Help": "Shared (personal)"},
        ],
    }
    frame.locator("#create-remote").click()
    frame.locator("#config-name").fill("onedrive")
    frame.locator("#config-backend").select_option("onedrive")
    frame.locator("#submit-config").click()
    question = frame.locator("#config-question")
    choices = question.get_by_role("combobox")
    playwright.expect(choices).to_be_visible()
    assert choices.evaluate("element => element.tagName") == "SELECT"
    assert choices.locator("option").evaluate_all(
        "options => options.slice(0, 3).map(option => option.value)"
    ) == ["drive-archive", "drive-main", "drive-shared"]
    value = question.locator('[data-option="config_driveid"]')
    playwright.expect(value).to_have_value(default)
    if default == "drive-archive":
        playwright.expect(choices).to_have_value(default)
        playwright.expect(value).not_to_be_visible()
    else:
        playwright.expect(value).to_be_visible()
    if selection == "custom":
        choices.select_option(label="手动输入…")
        playwright.expect(value).to_be_visible()
        value.fill(answer)
    elif selection:
        choices.select_option(selection)
        playwright.expect(value).to_have_value(selection)
    assert frame.locator("body").evaluate("e => e.scrollWidth <= window.innerWidth")
    frame.locator("#submit-config").click()
    playwright.expect(frame.locator("#submit-config")).to_have_text("保存连接")
    submitted = next(body for action, body in posts if action == "config-answer")
    assert submitted["answer"] == answer


def test_oauth_question_and_official_gui_share_configuration_controls(browser_page):
    page, frame, state, posts = browser_page
    frame.locator("#create-remote").click()
    frame.locator("#config-name").fill("onedrive")
    frame.locator("#config-backend").select_option("onedrive")
    frame.locator("#submit-config").click()
    playwright.expect(frame.locator("#config-question")).to_contain_text(
        'rclone authorize "onedrive"'
    )
    frame.locator('#config-question [data-option="config_token"]').fill("demo-token")
    frame.locator("#submit-config").click()
    playwright.expect(frame.locator("#submit-config")).to_have_text("保存连接")
    frame.locator("#submit-config").click()
    playwright.expect(frame.locator("#config-dialog")).not_to_be_visible()
    frame.locator("#open-gui").click()
    frame.locator("#start-gui").click()
    playwright.expect(frame.locator("#gui-login")).to_have_value(
        "http://127.0.0.1:5580/login?pass=test-gui-pass"
    )
    playwright.expect(frame.locator("#create-remote")).to_be_disabled()
    frame.locator("#stop-gui").click()
    playwright.expect(frame.locator("#gui-dialog")).not_to_be_visible()
    playwright.expect(frame.locator("#create-remote")).to_be_enabled()


def test_real_rclone_s3_metadata_renders_without_overflow(browser_page):
    binary = PLUGIN / "tests" / "tools" / "rclone.exe"
    if not binary.is_file():
        pytest.skip("Bundled rclone required for full metadata rendering")
    page, frame, state, posts = browser_page
    state["provider_catalog"] = json.loads(
        subprocess.check_output([str(binary), "config", "providers"])
    )
    page.set_viewport_size({"width": 390, "height": 1000})
    frame.locator("#create-remote").click()
    frame.locator("#config-backend").select_option("s3")
    frame.locator('[data-option="provider"]').fill("Other")
    playwright.expect(frame.locator('[data-option="endpoint"]')).to_be_visible()
    playwright.expect(frame.locator('[data-option="env_auth"]')).to_have_value("false")
    assert frame.locator("#config-fields .dynamic-field").count() > 5
    assert frame.locator("body").evaluate("e => e.scrollWidth <= window.innerWidth")
    output = PLUGIN / "tests" / "output"
    page.screenshot(path=str(output / "config-real-s3-mobile.png"))
    frame.locator("#close-config").click()


def test_mirror_progress_and_cancellation_are_visible_without_version_history(
    browser_page,
):
    page, frame, state, posts = browser_page
    state["cloud"]["connected"] = True
    state["tasks"] = [
        {
            "id": TASK_ID,
            "name": "镜像任务",
            "mode": "upload_mirror",
            "local_path": "/AstrBot/files",
            "remote_path": "files",
            "exclude": [],
            "enabled": True,
            "interval_minutes": 0,
            "next_run": None,
        }
    ]
    frame.locator("#refresh").click()
    frame.get_by_role("button", name="立即同步", exact=True).click()
    playwright.expect(frame.locator("#task-list")).to_contain_text("同步中")
    playwright.expect(frame.locator("#task-list")).to_contain_text("传输 1/2 个文件")
    playwright.expect(frame.locator("#task-list")).to_contain_text("已删除 1 个文件")
    assert frame.locator('[data-tab="cloud"]').count() == 0
    frame.get_by_role("button", name="取消同步", exact=True).click()
    frame.locator('[data-tab="runs"]').click()
    playwright.expect(frame.locator("#run-list")).to_contain_text("已取消")
    assert any(action == "cancel" for action, body in posts)


@pytest.mark.parametrize(
    "mode,label,upload,mirror",
    [
        ("upload", "上传更新", True, False),
        ("upload_mirror", "镜像到云端", True, True),
        ("download", "下载更新", False, False),
        ("download_mirror", "镜像到本地", False, True),
    ],
)
def test_four_modes_show_direction_and_submit_explicit_mode(
    browser_page, mode, label, upload, mirror
):
    page, frame, state, posts = browser_page
    state["cloud"]["connected"] = True
    frame.locator("#refresh").click()
    frame.locator("#new-task").click()
    frame.locator("#task-mode").select_option(mode)
    frame.locator("#task-name").fill(label)
    frame.locator("#task-local").fill(
        "/AstrBot/downloads" if not upload else "/AstrBot/files"
    )
    frame.locator("#task-remote").fill("files")
    playwright.expect(frame.locator("#local-path-label")).to_have_text(
        "本机来源（文件／目录）" if upload else "本机接收目录"
    )
    playwright.expect(frame.locator("#remote-path-label")).to_have_text(
        "云端目标子目录" if upload else "云端来源子目录"
    )
    if upload:
        playwright.expect(frame.locator("#local-examples")).to_be_visible()
    else:
        playwright.expect(frame.locator("#local-examples")).not_to_be_visible()
    direction = frame.locator("#task-destination").inner_text()
    assert (
        direction.startswith("/AstrBot/files →")
        if upload
        else direction.startswith("onedrive:AstrBotSync/files → /AstrBot/downloads")
    )
    frame.locator("#preview").click()
    playwright.expect(frame.locator("#preview-result")).to_contain_text(
        "将修改云端" if upload else "将修改本机"
    )
    playwright.expect(frame.locator("#preview-result")).to_contain_text(
        "删除 1" if mirror else "删除 0"
    )
    frame.locator("#save-task").click()
    playwright.expect(frame.locator("#task-dialog")).not_to_be_visible()
    assert state["tasks"][0]["mode"] == mode
    assert state["tasks"][0]["remote_path"] == "files"
    frame.locator("#task-list").get_by_role("button", name="编辑", exact=True).click()
    playwright.expect(frame.locator("#task-mode")).to_have_value(mode)
    assert any(action == "preview" and body["mode"] == mode for action, body in posts)
    if mode == "download_mirror":
        for width in (1280, 390):
            page.set_viewport_size({"width": width, "height": 1000})
            assert frame.locator("body").evaluate(
                "e => e.scrollWidth <= window.innerWidth"
            )
            page.screenshot(
                path=str(PLUGIN / "tests" / "output" / f"download-{width}.png")
            )
    frame.locator("#task-mode").select_option("download" if upload else "upload")
    playwright.expect(frame.locator("#preview-result")).not_to_be_visible()


def test_config_fields_cannot_be_edited_while_the_backend_is_refreshing(browser_page):
    page, frame, state, posts = browser_page
    frame.locator("#create-remote").click()
    provider = frame.locator('[data-option="provider"]')
    playwright.expect(provider).to_be_enabled()
    frame.locator("html").evaluate("""() => {
      const original = window.post;
      window.post = (action, body) => action === 'config-fields'
        ? new Promise(resolve => { window.releaseFields = () => resolve(original(action, body)); })
        : original(action, body);
    }""")
    frame.locator("#config-backend").select_option("s3")
    playwright.expect(provider).to_be_disabled()
    playwright.expect(frame.locator("#config-backend")).to_be_disabled()
    frame.locator("html").evaluate("() => window.releaseFields()")
    playwright.expect(provider).to_be_enabled()
    provider.select_option("Other")
    playwright.expect(frame.locator('[data-option="endpoint"]')).to_be_enabled()
    frame.locator('[data-option="endpoint"]').fill("https://s3.example.com")
    playwright.expect(frame.locator('[data-option="endpoint"]')).to_have_value(
        "https://s3.example.com"
    )
