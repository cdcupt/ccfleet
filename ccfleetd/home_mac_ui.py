"""Frontend views for the CC Fleet 'My machines' / 'Add a Mac' account page."""

import html

from . import usersite
from .bilingual import label

DOWNLOAD_URL = "/downloads/ccfleet-home-mac.command"
FALLBACK_COMMAND = "/usr/bin/python3 ~/Downloads/ccfleet-home-mac.command"
APPROVE_ACTION = "/account/machines/approve"
REMOVE_ACTION = "/account/machines/remove"
REFRESH_URL = "/account/machines"
ACCOUNT_URL = "/account"

CSS = """
.hm-page {
  color: var(--ink);
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
  padding-block: 0.25rem 2.5rem;
  line-height: 1.55;
}

.hm-page :where(a, button, input, summary):focus-visible {
  outline: 2px solid var(--acc);
  outline-offset: 2px;
}

.hm-back {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  align-self: flex-start;
  font-size: 0.9rem;
  color: var(--muted);
  text-decoration: none;
  border-radius: 4px;
  padding: 0.15rem 0;
}

.hm-back:hover {
  color: var(--ink);
  text-decoration: underline;
  text-underline-offset: 0.2em;
}

.hm-head {
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
  padding-bottom: 1.1rem;
  border-bottom: 1px solid var(--rule);
}

.hm-head h1 {
  margin: 0;
  font-size: clamp(1.45rem, 2.6vw, 1.85rem);
  line-height: 1.22;
  letter-spacing: -0.015em;
  font-weight: 650;
  color: var(--ink);
}

.hm-lead {
  margin: 0;
  font-size: 0.96rem;
  color: var(--muted);
  max-width: 62ch;
}

.hm-alert {
  padding: 0.85rem 1rem;
  border: 1px solid #c53030;
  border-left-width: 3px;
  border-radius: 8px;
  background: rgba(197, 48, 48, 0.08);
  background: color-mix(in srgb, #c53030 9%, transparent);
  color: var(--ink);
  font-size: 0.94rem;
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
  gap: 1.25rem;
}

.hm-step,
.hm-card {
  border: 1px solid var(--rule);
  border-radius: 10px;
  padding: 1.3rem 1.4rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.hm-step-head {
  display: flex;
  align-items: center;
  gap: 0.7rem;
}

.hm-step-num {
  flex: none;
  width: 1.65rem;
  height: 1.65rem;
  border-radius: 999px;
  border: 1px solid var(--acc);
  color: var(--acc);
  font-size: 0.85rem;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.hm-step-head h2 {
  margin: 0;
  font-size: 1.08rem;
  line-height: 1.35;
  font-weight: 600;
  color: var(--ink);
}

.hm-copy {
  margin: 0;
  font-size: 0.94rem;
  color: var(--muted);
  max-width: 62ch;
}

.hm-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.75rem;
}

.hm-fallback {
  border-top: 1px solid var(--rule);
  padding-top: 0.9rem;
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}

.hm-fallback-label {
  margin: 0;
  font-size: 0.85rem;
  color: var(--muted);
}

.hm-cmd {
  display: block;
  margin: 0;
  padding: 0.65rem 0.8rem;
  border: 1px solid var(--rule);
  border-radius: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 0.84rem;
  line-height: 1.45;
  color: var(--ink);
  background: rgba(127, 127, 127, 0.06);
  background: color-mix(in srgb, var(--ink) 4%, transparent);
  overflow-x: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  user-select: all;
}

.hm-form {
  display: flex;
  flex-direction: column;
  gap: 1rem;
  margin: 0;
}

.hm-fields {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 1rem;
}

.hm-field {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.hm-label {
  font-size: 0.9rem;
  font-weight: 550;
  color: var(--ink);
}

.hm-input {
  width: 100%;
  box-sizing: border-box;
  min-height: 2.65rem;
  padding: 0.55rem 0.75rem;
  border: 1px solid var(--rule);
  border-radius: 7px;
  background: transparent;
  color: var(--ink);
  font: inherit;
  font-size: 0.96rem;
}

.hm-input:focus {
  border-color: var(--acc);
}

.hm-input-code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.hm-hint {
  font-size: 0.82rem;
  color: var(--muted);
  line-height: 1.4;
}

.hm-notice {
  margin: 0;
  padding: 0.75rem 0.9rem;
  border: 1px solid var(--rule);
  border-left: 3px solid var(--acc);
  border-radius: 7px;
  font-size: 0.88rem;
  color: var(--muted);
  background: color-mix(in srgb, var(--acc) 5%, transparent);
}

.hm-card-top {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 0.65rem 1rem;
}

.hm-card-top h2 {
  margin: 0;
  font-size: 1.25rem;
  line-height: 1.3;
  font-weight: 650;
  color: var(--ink);
  overflow-wrap: anywhere;
}

.hm-status {
  display: inline-flex;
  align-items: center;
  gap: 0.45rem;
  padding: 0.25rem 0.7rem;
  border-radius: 999px;
  border: 1px solid var(--rule);
  font-size: 0.84rem;
  font-weight: 600;
  color: var(--ink);
}

.hm-dot {
  width: 0.5rem;
  height: 0.5rem;
  border-radius: 999px;
  flex: none;
  background: var(--muted);
}

.hm-status-online {
  border-color: rgba(22, 163, 74, 0.45);
  background: rgba(22, 163, 74, 0.08);
}

.hm-status-online .hm-dot {
  background: #16a34a;
}

.hm-status-waiting {
  border-color: rgba(217, 119, 6, 0.45);
  background: rgba(217, 119, 6, 0.08);
}

.hm-status-waiting .hm-dot {
  background: #d97706;
}

.hm-status-offline {
  border-color: var(--rule);
  color: var(--muted);
}

.hm-meta {
  margin: 0;
  padding-top: 0.9rem;
  border-top: 1px solid var(--rule);
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr));
  gap: 0.85rem 1.25rem;
}

.hm-meta dt {
  margin: 0 0 0.2rem;
  font-size: 0.8rem;
  color: var(--muted);
}

.hm-meta dd {
  margin: 0;
  font-size: 0.92rem;
  color: var(--ink);
  overflow-wrap: anywhere;
}

.hm-id {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 0.86rem;
}

.hm-remove {
  border: 1px solid var(--rule);
  border-radius: 10px;
  padding: 0.95rem 1.2rem;
}

.hm-remove summary {
  cursor: pointer;
  font-size: 0.92rem;
  font-weight: 550;
  color: var(--muted);
  list-style: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  border-radius: 4px;
}

.hm-remove summary::-webkit-details-marker {
  display: none;
}

.hm-remove summary::after {
  content: "";
  width: 0.45rem;
  height: 0.45rem;
  border-right: 1.5px solid currentColor;
  border-bottom: 1.5px solid currentColor;
  transform: rotate(45deg);
  transition: transform 0.15s ease;
  flex: none;
}

.hm-remove[open] summary::after {
  transform: rotate(-135deg);
}

.hm-remove summary:hover {
  color: var(--ink);
}

.hm-remove-body {
  margin-top: 0.95rem;
  padding-top: 0.95rem;
  border-top: 1px solid var(--rule);
  display: flex;
  flex-direction: column;
  gap: 0.9rem;
}

.hm-remove-form {
  margin: 0;
}

.hm-page .btn {
  min-height: 2.55rem;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.4rem;
  text-decoration: none;
  cursor: pointer;
}

.hm-page .hm-btn-primary {
  background: var(--acc);
  border: 1px solid var(--acc);
  color: #fff;
}

.hm-page .hm-btn-subtle {
  background: transparent;
  color: var(--ink);
  border: 1px solid var(--rule);
}

.hm-page .hm-btn-subtle:hover {
  border-color: var(--ink);
}

.hm-page .hm-btn-danger {
  background: transparent;
  color: #b91c1c;
  border: 1px solid rgba(185, 28, 28, 0.45);
}

.hm-page .hm-btn-danger:hover {
  background: rgba(185, 28, 28, 0.08);
  border-color: #b91c1c;
}

@media (prefers-color-scheme: dark) {
  .hm-alert {
    border-color: #f87171;
  }

  .hm-page .hm-btn-danger {
    color: #f87171;
    border-color: rgba(248, 113, 113, 0.45);
  }

  .hm-page .hm-btn-danger:hover {
    background: rgba(248, 113, 113, 0.12);
    border-color: #f87171;
  }
}

@media (prefers-reduced-motion: reduce) {
  .hm-remove summary::after {
    transition: none;
  }
}

@media (max-width: 600px) {
  .hm-step,
  .hm-card {
    padding: 1rem;
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


def _empty(csrf):
    instructions = _bi(
        "Download and open the connector on the Mac mini or MacBook you own.",
        "请在你自己的 Mac mini 或 MacBook 上下载并打开连接器。",
    )
    fallback = _bi("You can also open it from Terminal.", "也可以在终端中打开。")
    requirements = _bi(
        "The connector uses Python 3 from Apple’s Command Line Tools.",
        "连接器需要 Apple 命令行工具提供的 Python 3。",
    )
    consent = _bi(
        "Only approve the code on your own Mac. Your Fleet account can then use "
        "Claude signed in on that Mac.",
        "仅批准你自己 Mac 上显示的连接码。你的 Fleet 账户随后可使用该 Mac 已登录的 Claude。",
    )
    hint = _bi("Enter the ten characters shown by the connector.", "输入连接器显示的十位连接码。")
    return f"""
<ol class="hm-steps">
  <li class="hm-step">
    <div class="hm-step-head"><span class="hm-step-num" aria-hidden="true">1</span>
      <h2>{_bi("Open the connector on your Mac", "在 Mac 上打开连接器")}</h2></div>
    <p class="hm-copy">{instructions}</p>
    <div class="hm-actions"><a class="btn hm-btn-primary" href="{DOWNLOAD_URL}" download>
      {_bi("Download Mac connector", "下载 Mac 连接器")}</a></div>
    <details class="hm-fallback">
      <summary>{_bi("Opening the download", "如何打开下载文件")}</summary>
      <p class="hm-fallback-label">{fallback} {requirements}</p>
      <code class="hm-cmd">{FALLBACK_COMMAND}</code></details>
  </li>
  <li class="hm-step">
    <div class="hm-step-head"><span class="hm-step-num" aria-hidden="true">2</span>
      <h2>{_bi("Approve this Mac", "批准此 Mac")}</h2></div>
    <form class="hm-form" method="post" action="{APPROVE_ACTION}">
      <input type="hidden" name="csrf" value="{csrf}">
      <div class="hm-fields">
        <label class="hm-field"><span>{_bi("Connector code", "连接码")}</span>
          <input class="hm-input hm-input-code" name="code" required maxlength="11"
                 pattern="[A-Za-z0-9]{{5}}-?[A-Za-z0-9]{{5}}" autocapitalize="characters"
                 spellcheck="false" autocomplete="one-time-code" aria-describedby="hm-code-hint">
          <small id="hm-code-hint" class="hm-hint">{hint}</small></label>
        <label class="hm-field"><span>{_bi("Machine name", "设备名称")}</span>
          <input class="hm-input" name="name" maxlength="48" required autocomplete="off"></label>
      </div>
      <p class="hm-notice">{consent}</p>
      <div class="hm-actions"><button class="btn hm-btn-primary" type="submit">
        {_bi("Approve this Mac", "批准此 Mac")}</button></div>
    </form>
  </li>
</ol>"""


def _registered(machine, csrf):
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
    <span class="hm-status hm-status-{state}"><span class="hm-dot"></span>{status}</span></div>
  <p class="hm-copy">{note}</p>
  <dl class="hm-meta"><div><dt>{_bi("Fleet tunnel", "Fleet 隧道")}</dt><dd>{tunnel}</dd></div>
    <div><dt>{_bi("Model provider", "模型服务")}</dt><dd>{provider}</dd></div></dl>
  <div class="hm-actions"><a class="btn hm-btn-primary" href="/account">
    {_bi("Open your slots", "打开我的槽位")}</a>
    <a class="btn hm-btn-subtle" href="{REFRESH_URL}">{_bi("Refresh status", "刷新状态")}</a></div>
</section>
<details class="hm-remove"><summary>{_bi("Remove Mac", "移除 Mac")}</summary>
  <div class="hm-remove-body"><p class="hm-copy">{removal}</p>
    <form class="hm-remove-form" method="post" action="{REMOVE_ACTION}">
      <input type="hidden" name="csrf" value="{csrf}">
      <input type="hidden" name="node_id" value="{registration}">
      <button class="btn hm-btn-danger" type="submit">{_bi("Remove Mac", "移除 Mac")}</button>
    </form></div></details>"""


def page(viewer, machine=None, *, error=None):
    csrf = html.escape(viewer.csrf, quote=True)
    if machine is None:
        heading = _bi("Add a Mac", "添加 Mac")
        lead = _bi(
            "Connect a personal Mac over Wi-Fi or Ethernet. Your router needs no changes.",
            "通过 Wi-Fi 或以太网连接个人 Mac，无需更改路由器设置。",
        )
        content = _empty(csrf)
    else:
        heading = _bi("My machines", "我的设备")
        lead = _bi(
            "Manage your private Mac and check its connection.", "管理你的个人 Mac 并查看连接状态。"
        )
        content = _registered(machine, csrf)
    notice = f'<p class="hm-alert" role="alert">{_bi(*error)}</p>' if error else ""
    body = (
        f'<div class="hm-page"><a class="hm-back" href="{ACCOUNT_URL}">← '
        + _bi("Account", "账户")
        + '</a><header class="hm-head"><h1>'
        + heading
        + '</h1><p class="hm-lead">'
        + lead
        + "</p></header>"
        + notice
        + content
        + "</div>"
    )
    return usersite._shell("My machines", body, extra_css=CSS, width="home-machines", viewer=viewer)
