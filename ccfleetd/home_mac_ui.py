"""Frontend views for the CC Fleet 'My machines' / 'Add a Mac' account page."""

import html

from . import home_mac_motion, home_macs, usersite
from .bilingual import bi, label

APPLE_TOOLS_GUIDE = "https://developer.apple.com/documentation/xcode/installing-the-command-line-tools/"
DOWNLOAD_URL = "/downloads/ccfleet-home-mac.command"
FALLBACK_COMMAND = "/usr/bin/python3 ~/Downloads/ccfleet-home-mac.command"
REMOVE_ACTION = "/account/machines/remove"
REFRESH_URL = "/account/machines"
ACCOUNT_URL = "/account"

CSS = """
.hm-page {
  color: var(--ink);
  display: flex;
  flex-direction: column;
  gap: 20px;
  padding-block: 4px 36px;
  line-height: 1.6;
}

.hm-page :where(a, button, input, select, summary):focus-visible {
  outline: 2px solid var(--acc);
  outline-offset: 2px;
}

.hm-back {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  align-self: flex-start;
  min-height: 36px;
  font-size: 13.5px;
  font-weight: 600;
  color: var(--muted);
  text-decoration: none;
  border-radius: 6px;
}

.hm-back:hover {
  color: var(--acc);
  text-decoration: underline;
  text-underline-offset: 0.2em;
}

.hm-head {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  justify-content: space-between;
  gap: 14px 20px;
  padding-bottom: 20px;
  border-bottom: 1px solid var(--rule);
  animation: cf-rise .42s var(--ease) both;
}

.hm-head-copy {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}

.hm-eyebrow {
  margin: 0;
  font-size: 11.5px;
  font-weight: 700;
  letter-spacing: .09em;
  text-transform: uppercase;
  color: var(--acc);
}

.hm-head h1 {
  margin: 0;
  font-family: var(--serif);
  font-size: clamp(30px, 4.2vw, 42px);
  line-height: 1.12;
  letter-spacing: -.026em;
  font-weight: 500;
  color: var(--ink);
}

.hm-lead {
  margin: 0;
  font-size: 15px;
  line-height: 1.68;
  color: var(--muted);
  max-width: 64ch;
}

.hm-alert {
  margin: 0;
  padding: 13px 16px;
  border: 1px solid var(--bad-line);
  border-left: 3px solid var(--bad);
  border-radius: var(--radius-md);
  background: var(--bad-bg);
  color: var(--bad);
  font-size: 14px;
  line-height: 1.6;
}

.hm-alert p {
  margin: 0;
}

.hm-steps {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.hm-step,
.hm-card {
  background: var(--panel);
  border: 1px solid var(--rule);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  padding: 24px 28px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  animation: cf-rise .44s var(--ease) both;
}

.hm-step-head {
  display: flex;
  align-items: center;
  gap: 11px;
}

.hm-step-num {
  flex: none;
  width: 28px;
  height: 28px;
  border-radius: 999px;
  background: var(--cta);
  color: var(--on-cta);
  font-size: 13px;
  font-weight: 680;
  font-variant-numeric: tabular-nums;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.hm-step-head h2,
.hm-card > h2 {
  margin: 0;
  font-family: var(--serif);
  font-size: 22px;
  line-height: 1.28;
  letter-spacing: -.018em;
  font-weight: 500;
  color: var(--ink);
}

.hm-copy {
  margin: 0;
  font-size: 14.5px;
  line-height: 1.68;
  color: var(--muted);
  max-width: 64ch;
}

.hm-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}

.hm-fallback,
.hm-remove {
  border-top: 1px solid var(--rule-soft);
  padding-top: 14px;
  margin-top: 2px;
}

.hm-fallback > summary,
.hm-remove > summary {
  cursor: pointer;
  font-size: 13.5px;
  font-weight: 600;
  color: var(--muted);
  list-style: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 4px 0;
  border-radius: 6px;
  transition: color .15s var(--ease);
}

.hm-fallback > summary::-webkit-details-marker,
.hm-remove > summary::-webkit-details-marker {
  display: none;
}

.hm-fallback > summary::marker,
.hm-remove > summary::marker {
  content: "";
}

.hm-fallback > summary::after,
.hm-remove > summary::after {
  content: "";
  width: 7px;
  height: 7px;
  border-right: 1.5px solid currentColor;
  border-bottom: 1.5px solid currentColor;
  transform: rotate(45deg);
  transition: transform .18s var(--ease);
  flex: none;
  margin-right: 2px;
}

.hm-fallback[open] > summary::after,
.hm-remove[open] > summary::after {
  transform: rotate(-135deg);
}

.hm-fallback > summary:hover,
.hm-remove > summary:hover {
  color: var(--ink);
}

.hm-fallback-body,
.hm-remove-body {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.hm-fallback-label {
  margin: 0;
  font-size: 13.5px;
  line-height: 1.6;
  color: var(--muted);
}

.hm-cmd {
  display: block;
  margin: 0;
  padding: 11px 14px;
  border: 1px solid var(--rule);
  border-radius: 10px;
  font-family: var(--mono);
  font-size: 13px;
  line-height: 1.55;
  color: var(--ink);
  background: var(--inset);
  overflow-x: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  user-select: all;
}

.hm-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
  margin: 0;
}

.hm-fields {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px;
}

.hm-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.hm-label {
  font-size: 13.5px;
  font-weight: 620;
  color: var(--ink);
}

.hm-input {
  width: 100%;
  box-sizing: border-box;
  min-height: 44px;
  padding: 10px 13px;
  border: 1px solid var(--rule);
  border-radius: 10px;
  background: var(--panel);
  color: var(--ink);
  font: inherit;
  font-size: 15px;
  transition: border-color .15s var(--ease), box-shadow .15s var(--ease);
}

.hm-input::placeholder {
  color: var(--muted);
}

.hm-input:focus {
  border-color: var(--acc);
  box-shadow: 0 0 0 3px var(--acc-soft);
  outline: none;
}

.hm-input-code {
  font-family: var(--mono);
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.hm-hint {
  font-size: 12.5px;
  color: var(--muted);
  line-height: 1.5;
}

.hm-notice {
  margin: 0;
  padding: 13px 16px;
  border: 1px solid var(--acc-line);
  border-left: 3px solid var(--acc);
  border-radius: var(--radius-md);
  font-size: 13.5px;
  line-height: 1.65;
  color: var(--muted);
  background: var(--acc-soft);
}

.hm-notice strong {
  color: var(--ink);
}

.hm-card-top {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 10px 14px;
}

.hm-card-top h2 {
  margin: 0;
  font-family: var(--serif);
  font-size: 22px;
  line-height: 1.28;
  letter-spacing: -.018em;
  font-weight: 500;
  color: var(--ink);
  overflow-wrap: anywhere;
}

.hm-status {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 4px 12px;
  border-radius: var(--radius-pill);
  border: 1px solid var(--rule);
  background: var(--surface-subtle);
  font-size: 12.5px;
  font-weight: 620;
  color: var(--ink);
  white-space: nowrap;
}

.hm-dot {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  flex: none;
  background: var(--muted);
}

.hm-status-online {
  border-color: var(--ok-line);
  background: var(--ok-bg);
  color: var(--ok);
}

.hm-status-online .hm-dot {
  background: var(--ok);
  animation: cf-pulse 2.6s var(--ease) infinite;
}

.hm-status-waiting {
  border-color: var(--warn-line);
  background: var(--warn-bg);
  color: var(--warn);
}

.hm-status-waiting .hm-dot {
  background: var(--warn);
}

.hm-status-offline {
  border-color: var(--rule);
  background: var(--inset);
  color: var(--muted);
}

.hm-meta {
  margin: 0;
  padding-top: 14px;
  border-top: 1px solid var(--rule-soft);
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr));
  gap: 12px 20px;
}

.hm-meta dt {
  margin: 0 0 3px;
  font-size: 12px;
  font-weight: 560;
  color: var(--muted);
}

.hm-meta dd {
  margin: 0;
  font-size: 14px;
  font-weight: 550;
  color: var(--ink);
  overflow-wrap: anywhere;
}

.hm-id {
  font-family: var(--mono);
  font-size: 13px;
}

.hm-remove-form {
  margin: 0;
}

.hm-page .btn {
  min-height: 42px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  text-decoration: none;
  cursor: pointer;
}

.hm-page .hm-btn-primary {
  background: var(--cta);
  border: 1px solid var(--cta);
  color: var(--on-cta);
}

.hm-page .hm-btn-primary:hover {
  opacity: .92;
}

.hm-page .hm-btn-subtle {
  background: var(--panel);
  color: var(--ink);
  border: 1px solid var(--rule);
}

.hm-page .hm-btn-subtle:hover {
  border-color: var(--ink);
  background: var(--surface-subtle);
}

.hm-page .hm-btn-danger {
  background: var(--panel);
  color: var(--bad);
  border: 1px solid var(--bad-line);
}

.hm-page .hm-btn-danger:hover {
  background: var(--bad-bg);
  border-color: var(--bad);
}

@media (prefers-reduced-motion: reduce) {
  .hm-head,
  .hm-step,
  .hm-card,
  .hm-status-online .hm-dot {
    animation: none !important;
  }

  .hm-fallback > summary::after,
  .hm-remove > summary::after {
    transition: none;
  }
}

@media (max-width: 640px) {
  .hm-head {
    align-items: flex-start;
  }

  .hm-step,
  .hm-card {
    padding: 20px 18px;
  }

  .hm-fields {
    grid-template-columns: 1fr;
  }

  .hm-actions .btn,
  .hm-remove-form .btn {
    width: 100%;
  }
}
"""


def _bi(en: str, zh: str) -> str:
    return label(en, zh, inline=True)


def _empty(csrf, *, confirm=False, account_email=""):
    prerequisite = bi(
        '<p class="hm-copy"><strong>New Mac?</strong> The connector uses Python 3 from '
        'Apple’s Command Line Tools. If the tools are missing, install them first. '
        f'<a href="{APPLE_TOOLS_GUIDE}">Apple’s installer instructions</a></p>',
        '<p class="hm-copy"><strong>第一次在这台 Mac 上设置？</strong>连接器需要 Apple 命令行工具'
        '提供的 Python 3。如果尚未安装，请先安装。'
        f'<a href="{APPLE_TOOLS_GUIDE}">查看 Apple 安装说明</a></p>',
    )
    instructions = _bi(
        "Download and open the signed connector on the Mac you want to add. "
        "A Terminal window may open. Continue in your browser to approve this Mac. "
        "The Fleet CLI is optional.",
        "在要添加的 Mac 上下载并打开已签名的连接器。启动时可能会打开“终端”窗口。"
        "然后回到浏览器批准这台 Mac。Fleet CLI 为可选工具。",
    )
    fallback = _bi("If double-clicking the download does not open it, run this in Terminal.",
                   "如果双击下载的连接器无法打开，请在“终端”中运行以下命令。")
    consent = _bi(
        "Approve only the code shown on your own Mac. You can register the Mac now. "
        "To use Claude through this slot, install Claude Code and sign in on that Mac.",
        "请只批准自己 Mac 上显示的连接码。你可以先注册 Mac；要通过这个槽位使用 Claude，"
        "请在该 Mac 上安装并登录 Claude Code。",
    )
    hint = _bi("Enter the ten characters shown by the connector.", "输入连接器显示的十位连接码。")
    action = home_macs.ADD_ACTION
    open_step = f"""
  <li class="hm-step">
    <div class="hm-step-head"><span class="hm-step-num" aria-hidden="true">1</span>
      <h2>{_bi("Open the connector on your Mac", "在 Mac 上打开连接器")}</h2></div>
    {prerequisite}
    <p class="hm-copy">{instructions}</p>
    <div class="hm-actions"><a class="btn primary hm-btn-primary" href="{DOWNLOAD_URL}" download>
      {_bi("Download Mac connector", "下载 Mac 连接器")}</a></div>
    <details class="hm-fallback">
      <summary>{_bi("Opening the download", "如何打开下载文件")}</summary>
      <div class="hm-fallback-body">
        <p class="hm-fallback-label">{fallback}</p>
        <code class="hm-cmd">{FALLBACK_COMMAND}</code>
      </div></details>
  </li>
"""
    approve_badge = "" if confirm else '<span class="hm-step-num" aria-hidden="true">2</span>'
    return (
        '<ol class="hm-steps">'
        + ("" if confirm else open_step)
        + f"""
  <li class="hm-step">
    <div class="hm-step-head">{approve_badge}
      <h2>{_bi("Approve this Mac", "批准此 Mac")}</h2></div>
    <form class="hm-form" method="post" action="{action}">
      <input type="hidden" name="csrf" value="{csrf}">
      <div class="hm-fields">
        <label class="hm-field"><span class="hm-label">{_bi("Connector code", "连接码")}</span>
          <input class="hm-input hm-input-code" data-hm-code name="code" required maxlength="11"
                 pattern="[A-Za-z0-9]{{5}}-?[A-Za-z0-9]{{5}}" autocapitalize="characters"
                 spellcheck="false" autocomplete="one-time-code" placeholder="ABCDE-FGHIJ"
                 aria-describedby="hm-code-hint">
          <small id="hm-code-hint" class="hm-hint">{hint}</small></label>
        <label class="hm-field"><span class="hm-label">{_bi("Machine name", "设备名称")}</span>
          <input class="hm-input" name="name" maxlength="48" required autocomplete="off"
                 placeholder="e.g. Studio Mac" data-placeholder-en="e.g. Studio Mac"
                 data-placeholder-zh="例如：书房 Mac"></label>
      </div>
      <p class="hm-notice">{consent}<br>{_bi("Fleet account:", "Fleet 账户：")}
        <strong>{html.escape(account_email)}</strong></p>
      <div class="hm-actions"><button class="btn primary hm-btn-primary" type="submit">
        {_bi("Approve this Mac", "批准此 Mac")}</button></div>
    </form>
  </li>
</ol>"""
    )


def _registered(machine, csrf):
    from .dedicated_slots import slot_url

    states = {
        "online": (
            _bi("Online", "在线"),
            _bi("The connector recently checked in.", "连接器刚刚连接。"),
        ),
        "waiting": (
            _bi("Waiting for Mac", "等待 Mac"),
            _bi("Waiting for the first check-in after approval.", "已批准，等待首次连接。"),
        ),
        "offline": (
            _bi("Offline", "离线"),
            _bi(
                "Wake the Mac, check Wi-Fi, and reopen the connector.",
                "请唤醒 Mac，检查 Wi-Fi，然后重新打开连接器。",
            ),
        ),
    }
    state = machine["state"]
    status, note = states[state]
    provider = {
        "ready": _bi("Claude sign-in verified", "Claude 登录已验证"),
        "sign_in": _bi("Sign in to Claude on the Mac", "请在 Mac 上登录 Claude"),
        "unconfirmed": _bi("Waiting for a current Claude report", "等待最新的 Claude 状态报告"),
    }[machine["provider"]]
    tunnel = (
        _bi("A connection has been opened", "已建立过连接")
        if machine["tunnel_seen"]
        else _bi("Waiting for first use", "等待首次使用")
    )
    name = html.escape(machine["name"])
    registration = html.escape(machine["id"], quote=True)
    removal = _bi(
        "Remove this Mac from your account and end its Fleet connections?",
        "要从账户移除此 Mac 并断开其 Fleet 连接吗？",
    )
    return f"""
<section class="hm-card">
  <div class="hm-card-top"><h2>{name}</h2>
    <span class="hm-status hm-status-{state}"><span class="hm-dot" aria-hidden="true"></span>
      {status}</span></div>
  <p class="hm-copy">{note}</p>
  <dl class="hm-meta"><div><dt>{_bi("Fleet tunnel", "Fleet 隧道")}</dt><dd>{tunnel}</dd></div>
    <div><dt>{_bi("Model provider", "模型服务")}</dt><dd>{provider}</dd></div></dl>
  <div class="hm-actions">
    <a class="btn primary hm-btn-primary" href="{slot_url(machine["slot_id"])}">
      {_bi("Use this slot", "使用此槽位")}</a>
    <a class="btn hm-btn-subtle" href="{REFRESH_URL}">{_bi("Refresh status", "刷新状态")}</a></div>
  <details class="hm-remove"><summary>{_bi("Remove Mac", "移除 Mac")}</summary>
    <div class="hm-remove-body"><p class="hm-copy">{removal}</p>
      <form class="hm-remove-form" method="post" action="{REMOVE_ACTION}">
        <input type="hidden" name="csrf" value="{csrf}">
        <input type="hidden" name="node_id" value="{registration}">
        <button class="btn hm-btn-danger" type="submit">{_bi("Remove Mac", "移除 Mac")}</button>
      </form></div></details>
</section>"""


CONNECTION_CSS = home_mac_motion.CSS
connection = home_mac_motion.panel
script_tag = home_mac_motion.script_tag


def enrollment(viewer, machines, *, confirm=False, add=False, error=None, navigation=""):
    csrf = html.escape(viewer.csrf, quote=True) if viewer else ""
    header_action = ""
    if confirm:
        page_title = "Approve this Mac"
        title = _bi("Approve this Mac", "批准此 Mac")
        lead = _bi(
            "Verify the ten-character code shown by the connector on your Mac "
            "and choose a name for this private slot.",
            "核对 Mac 连接器显示的十位连接码，并为这个专属槽位命名。",
        )
    elif add or not machines:
        page_title = "Add my Mac"
        title = _bi("Add my Mac", "添加我的 Mac")
        lead = _bi(
            "Connect your own Mac mini or MacBook as a private Fleet slot over an "
            "outbound connection—no router port forwarding or Remote Login required.",
            "通过主动外连将你自己的 Mac mini 或 MacBook 添加为专属 Fleet 槽位，"
            "无需路由器端口转发或开启远程登录。",
        )
    else:
        page_title = "My machines"
        title = _bi("My machines", "我的设备")
        lead = _bi(
            "Personal Macs connected to your Fleet account. Each Mac keeps its own "
            "private slot and does not count against your hosted slot allowance.",
            "已连接到你 Fleet 账户的个人 Mac。每台 Mac 拥有独立专属槽位，不占用托管槽位额度。",
        )
        header_action = (
            '<a class="btn primary hm-btn-primary" href="'
            + home_macs.ADD
            + '">'
            + _bi("Add my Mac", "添加我的 Mac")
            + "</a>"
        )
    if not viewer:
        main = (
            '<section class="hm-card"><p class="hm-copy">'
            + _bi(
                "Sign in to your Fleet account to verify the code shown on your Mac.",
                "请登录你的 Fleet 账户，以核对并批准 Mac 上显示的连接码。",
            )
            + '</p><div class="hm-actions">'
            '<a class="btn primary hm-btn-primary" '
            'href="/auth/google/start?next=/account/machines/confirm">'
            + _bi("Sign in to approve this Mac", "登录并批准此 Mac")
            + "</a></div></section>"
        )
    elif add or confirm or not machines:
        main = _empty(csrf, confirm=confirm, account_email=viewer.account.get("email", ""))
    else:
        main = "".join(_registered(m, csrf) for m in machines)
    back_href = REFRESH_URL if (viewer and machines and add) else ACCOUNT_URL
    back_text = (
        _bi("Back to my machines", "返回我的设备")
        if back_href == REFRESH_URL
        else _bi("Account", "账户")
    )
    back_link = (
        ""
        if navigation
        else f'<a class="hm-back" href="{back_href}"><span aria-hidden="true">←</span>'
        + back_text
        + "</a>"
    )
    alert = '<p class="hm-alert" role="alert">' + _bi(*error) + "</p>" if error else ""
    body = (
        navigation
        + f'<div class="hm-page">{back_link}<header class="hm-head"><div class="hm-head-copy">'
        '<p class="hm-eyebrow">'
        + _bi("Personal Macs", "个人 Mac")
        + "</p><h1>"
        + title
        + f'</h1><p class="hm-lead">{lead}</p></div>{header_action}</header>'
        + alert
        + main
        + "</div>"
        + script_tag()
    )
    return usersite._shell(page_title, body, extra_css=CSS, viewer=viewer, width="home-machines")
