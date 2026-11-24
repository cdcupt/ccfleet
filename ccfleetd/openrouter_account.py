"""Self-service keys, models, and usage receipts for the Fleet OpenRouter gateway."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from html import escape
from typing import Any, Optional

from . import usersite, wallet, wallet_routes
from .bilingual import bi, label
from .openrouter import PAGE

ENTRY_CSS = """
.or-entry{margin:20px 0;padding:24px 28px;border:1px solid var(--rule);border-radius:var(--radius);
background:var(--panel);box-shadow:var(--shadow);display:grid;gap:12px;
animation:cf-rise .42s var(--ease) both}
.or-entry-top{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px}
.or-eyebrow{margin:0;color:var(--acc);font-size:11.5px;font-weight:700;letter-spacing:.09em;
text-transform:uppercase}
.or-badges{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.or-entry h2{margin:0;font-family:var(--serif);font-size:22px;font-weight:500;line-height:1.25;
letter-spacing:-.018em}
.or-entry p{margin:0;font-size:14px;line-height:1.7;color:var(--muted);max-width:68ch}
.or-entry-actions{display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin-top:4px}
.or-entry-actions .btn{min-height:40px}
.or-link{font-size:13.5px;font-weight:560;color:var(--acc);text-decoration:none}
.or-link:hover{text-decoration:underline;text-underline-offset:3px}
@media(max-width:640px){.or-entry{padding:20px 18px}}
"""

PAGE_CSS = ENTRY_CSS + """
.or-page{display:grid;gap:22px;min-width:0}
.or-page p{overflow-wrap:anywhere}
.or-head{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;
gap:14px 20px;margin:0}
.or-head h1{margin:6px 0 8px;font-family:var(--serif);font-size:clamp(32px,4.4vw,44px);
font-weight:500;line-height:1.1;letter-spacing:-.028em}
.or-head .sub{margin:0;font-size:15px;line-height:1.68;color:var(--muted);max-width:64ch}
.or-issued{padding:22px 26px;border:1px solid var(--ok-line);border-radius:var(--radius);
background:var(--ok-bg);box-shadow:var(--shadow-sm);display:grid;gap:12px;
animation:cf-rise .36s var(--ease) both}
.or-issued-head{display:flex;flex-wrap:wrap;align-items:center;gap:10px}
.or-issued h2{margin:0;font-family:var(--serif);font-size:21px;font-weight:500;
letter-spacing:-.015em;color:var(--ink)}
.or-issued p{margin:0;font-size:13.5px;line-height:1.68;color:var(--muted);max-width:72ch}
.or-grid{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:22px;align-items:start}
.or-main{display:grid;gap:20px;min-width:0}
.or-rail{display:grid;gap:18px;min-width:0;position:sticky;top:84px}
.or-card{background:var(--panel);border:1px solid var(--rule);border-radius:var(--radius);
box-shadow:var(--shadow);padding:24px 26px;min-width:0;display:grid;gap:14px;
animation:cf-rise .44s var(--ease) both}
.or-main>.or-card:nth-child(2){animation-delay:.05s}
.or-main>.or-card:nth-child(3){animation-delay:.1s}
.or-main>.or-card:nth-child(4){animation-delay:.14s}
.or-card-head{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;
gap:8px 14px}
.or-card h2{margin:0;font-family:var(--serif);font-size:22px;font-weight:500;line-height:1.25;
letter-spacing:-.018em}
.or-card-sub{margin:0;font-size:13.5px;line-height:1.68;color:var(--muted)}
.or-key-actions{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:4px}
.or-key-actions form{margin:0}
.or-key-actions .btn{min-height:42px;white-space:normal;text-align:center}
.or-btn-revoke{color:var(--bad);border-color:var(--bad-line);background:var(--panel)}
.or-btn-revoke:hover{background:var(--bad-bg);border-color:var(--bad)}
.or-note{margin:0;font-size:12.5px;line-height:1.68;color:var(--muted)}
.or-code-box{border:1px solid var(--rule);border-radius:12px;overflow:hidden;
background:var(--inset);min-width:0}
.or-code-bar{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;
gap:8px 12px;padding:9px 14px;border-bottom:1px solid var(--rule);
font:11.5px/1.4 var(--mono);color:var(--muted)}
.or-code-box pre{margin:0;padding:14px 16px;background:transparent;border:0;border-radius:0;
font-size:12.5px;line-height:1.7;white-space:pre-wrap;overflow-wrap:anywhere;
word-break:break-word;user-select:all;-webkit-user-select:all}
.or-code-box pre code{background:transparent;padding:0;border:0;white-space:pre-wrap;
overflow-wrap:anywhere;word-break:break-word}
.or-copy{min-height:30px;padding:4px 10px;font:600 12px/1.3 var(--sans);flex:none}
.or-copy[hidden]{display:none}
.or-steps{list-style:none;margin:0;padding:0;counter-reset:or-step;display:grid;gap:18px}
.or-step{padding-top:18px;border-top:1px solid var(--rule-soft);counter-increment:or-step;
display:grid;gap:10px;min-width:0}
.or-step:first-child{padding-top:0;border-top:0}
.or-step h3{margin:0;display:flex;align-items:center;gap:10px;font-size:15px;font-weight:620;
line-height:1.45;letter-spacing:-.01em}
.or-step h3::before{content:counter(or-step);display:grid;place-items:center;flex:none;
width:25px;height:25px;border-radius:50%;background:var(--cta);color:var(--on-cta);
font-size:12px;font-weight:650;font-variant-numeric:tabular-nums}
.or-step p{margin:0;font-size:13.5px;line-height:1.68;color:var(--muted)}
.or-callout{padding:14px 16px;border-radius:12px;border:1px solid var(--acc-line);
background:var(--acc-soft);font-size:13px;line-height:1.68;color:var(--ink)}
.or-callout strong{color:var(--ink)}
.or-sheet{overflow-x:auto;max-width:100%;min-width:0;border:1px solid var(--rule-soft);
border-radius:12px}
.or-table{width:100%;min-width:440px;border-collapse:collapse;font-size:13px}
.or-table th,.or-table td{padding:11px 14px;text-align:left;vertical-align:top;
border-bottom:1px solid var(--rule-soft);white-space:normal}
.or-table th{font-size:12px;font-weight:620;color:var(--muted);background:var(--surface-subtle)}
.or-table tr:last-child td{border-bottom:0}
.or-table th.num,.or-table td.num{text-align:right;font-variant-numeric:tabular-nums;
white-space:nowrap}
.or-table .pill{white-space:nowrap}
.or-model-cell{display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px}
.or-model-cell code{font-size:12.5px;overflow-wrap:anywhere}
.or-tag{display:inline-flex;align-items:center;padding:2px 8px;border-radius:99px;
font-size:11px;font-weight:620;line-height:1.4;white-space:nowrap;border:1px solid var(--rule);
background:var(--inset);color:var(--muted)}
.or-tag.default{background:var(--acc-soft);border-color:var(--acc-line);color:var(--acc)}
.or-tag.recent{background:var(--slot-assigned-bg);border-color:var(--slot-assigned-line);
color:var(--slot-assigned)}
.or-tag.covered{background:var(--ok-bg);border-color:var(--ok-line);color:var(--ok)}
.or-stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
.or-stat{padding:13px 14px;border:1px solid var(--rule-soft);border-radius:12px;
background:var(--surface-subtle);min-width:0}
.or-stat-label{display:block;font-size:11.5px;color:var(--muted);margin-bottom:4px}
.or-stat-value{display:block;font-family:var(--serif);font-size:24px;font-weight:500;
line-height:1.15;letter-spacing:-.02em;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.or-req-id{display:block;margin-top:3px;font-size:11.5px;color:var(--muted);
font-family:var(--mono);overflow-wrap:anywhere}
.or-billing-detail{display:grid;gap:2px;justify-items:end;text-align:right;
font-variant-numeric:tabular-nums}
.or-billing-detail small{font-size:11.5px;color:var(--muted)}
.or-empty{padding:26px 20px;border:1px dashed var(--rule);border-radius:12px;
background:var(--surface-subtle);text-align:center;display:grid;gap:6px;justify-items:center}
.or-empty strong{font-size:14.5px;color:var(--ink)}
.or-empty p{margin:0;font-size:13px;color:var(--muted);max-width:46ch}
.or-balance-card{padding:24px;border:1px solid var(--rule);border-radius:var(--radius);
background:var(--panel);box-shadow:var(--shadow);display:grid;gap:14px;
animation:cf-rise .44s var(--ease) both}
.or-balance-card.admin-covered{border-color:var(--ok-line)}
.or-figure{margin:0;font-family:var(--serif);font-size:clamp(34px,4vw,44px);font-weight:500;
line-height:1.08;letter-spacing:-.03em;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.or-figure-sub{margin:-8px 0 0;font-size:13px;color:var(--muted)}
.or-hold-note{margin:0;padding:10px 12px;border-radius:10px;border:1px solid var(--warn-line);
background:var(--warn-bg);color:var(--warn);font-size:12.5px;line-height:1.55}
.or-topup-btn{width:100%;min-height:44px;justify-content:center}
.or-meta{display:grid;gap:10px;margin:2px 0 0;padding-top:14px;
border-top:1px solid var(--rule-soft)}
.or-meta div{display:flex;justify-content:space-between;align-items:baseline;gap:12px;min-width:0}
.or-meta dt{font-size:12.5px;color:var(--muted);flex:none}
.or-meta dd{margin:0;font-size:12.5px;font-weight:600;text-align:right;overflow-wrap:anywhere;
font-variant-numeric:tabular-nums}
.or-trust{padding:20px 22px;border:1px solid var(--rule);border-radius:var(--radius);
background:var(--surface-subtle);display:grid;gap:10px}
.or-trust h3{margin:0;font-size:13.5px;font-weight:650;color:var(--ink)}
.or-trust ul{margin:0;padding-left:18px;display:grid;gap:8px;font-size:12.5px;line-height:1.65;
color:var(--muted)}
.or-details{border:1px solid var(--rule);border-radius:var(--radius);background:var(--panel)}
.or-details>summary{padding:16px 20px;font-size:13.5px;font-weight:600;cursor:pointer}
.or-details[open]>summary{border-bottom:1px solid var(--rule-soft)}
.or-details-body{padding:14px 20px 18px;display:grid;gap:10px;font-size:13px;line-height:1.7;
color:var(--muted)}
.or-details-body p{margin:0}
.or-more-models{margin-top:4px;border:1px solid var(--rule-soft);border-radius:12px;
background:var(--surface-subtle)}
.or-more-models>summary{padding:12px 16px;font-size:13px;font-weight:600;cursor:pointer}
.or-more-models[open]>summary{border-bottom:1px solid var(--rule-soft)}
.or-more-models .or-sheet{border:0;border-radius:0 0 12px 12px}
@media(max-width:960px){.or-grid{grid-template-columns:minmax(0,1fr)}
.or-rail{position:static}}
@media(max-width:640px){.or-card,.or-balance-card,.or-issued{padding:20px 16px}
.or-stats{grid-template-columns:minmax(0,1fr)}
.or-key-actions{align-items:stretch}
.or-key-actions form,.or-key-actions .btn{width:100%}
.or-table{min-width:380px}
.or-table th,.or-table td{padding:10px 10px}}
@media(prefers-reduced-motion:reduce){.or-entry,.or-issued,.or-card,.or-balance-card{
animation:none!important}}
"""


def _is_admin_covered(cfg: Any, account: Optional[Mapping[str, Any]]) -> bool:
    admin_id = getattr(cfg, "openrouter_admin_account_id", "") or ""
    return bool(account and admin_id and account.get("id") == admin_id
                and account.get("role") == "admin")


def _account_state(store: Any, owner: str) -> tuple[Optional[bool], Decimal]:
    """Read key presence (never hash/secret) and fractional-cent carry safely."""
    lock = getattr(store, "_lock", None)
    conn = getattr(store, "_conn", None)
    if lock is None or conn is None:
        return None, Decimal(0)
    try:
        with lock:
            row = conn.execute(
                "SELECT token_hash IS NOT NULL, remainder_usd "
                "FROM openrouter_accounts WHERE account_id=?",
                (owner,),
            ).fetchone()
    except Exception:
        return None, Decimal(0)
    if row is None:
        return False, Decimal(0)
    try:
        carry = Decimal(str(row[1] or "0"))
    except Exception:
        carry = Decimal(0)
    return bool(row[0]), carry


def _usd(amount: Decimal) -> str:
    """Format a USD Decimal cleanly with at least 2 decimals while preserving sub-cent precision."""
    normalized = format(amount.normalize(), "f")
    if "." not in normalized:
        return f"${normalized}.00"
    whole, frac = normalized.split(".", 1)
    if len(frac) < 2:
        frac = frac.ljust(2, "0")
    return f"${whole}.{frac}"


def _rate_usd(amount: Decimal) -> str:
    """Format a per-million-token USD price ceiling compactly."""
    normalized = format(amount.normalize(), "f")
    if "." in normalized:
        whole, frac = normalized.split(".", 1)
        if len(frac) == 1:
            return f"${whole}.{frac}0"
    return f"${normalized}"


def _utc_time(ts: Any) -> str:
    try:
        moment = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return "—"
    iso = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    text = moment.strftime("%Y-%m-%d %H:%M UTC")
    return f'<time data-local datetime="{iso}">{text}</time>'


def _code_box(title_en: str, title_zh: str, content: str, *,
              copy_en: str = "Copy", copy_zh: str = "复制") -> str:
    return (
        '<div class="or-code-box" data-code-box>'
        '<div class="or-code-bar"><span>'
        + label(title_en, title_zh, inline=True)
        + '</span><button type="button" class="btn quiet or-copy" data-copy-code hidden>'
        + label(copy_en, copy_zh, inline=True)
        + "</button></div>"
        + "<pre><code>"
        + escape(content)
        + "</code></pre></div>"
    )


def entry(cfg: Any, account: Optional[Mapping[str, Any]], store: Any) -> str:
    """Account-dashboard entry card linking signed-in holders to /account/openrouter."""
    if not getattr(cfg, "openrouter_models", "") or getattr(cfg, "broker_only", False) is True:
        return ""
    covered = _is_admin_covered(cfg, account)
    has_key: Optional[bool] = None
    if account is not None:
        has_key, _ = _account_state(store, account["id"])
    pricing_badge = (
        '<span class="pill ok">'
        + label("Admin covered · $0.00 Fleet charge", "管理员覆盖 · Fleet 扣费 $0.00", inline=True)
        + "</span>"
        if covered
        else '<span class="pill">'
        + label("OpenRouter + 5% · Fleet credit", "OpenRouter + 5% · Fleet 余额", inline=True)
        + "</span>"
    )
    key_badge = (
        '<span class="pill assigned">'
        + label("Key active", "密钥已启用", inline=True)
        + "</span>"
        if has_key
        else ""
    )
    topup = (
        '<a class="or-link" href="/account/topup">'
        + label("Add Fleet credit", "充值 Fleet 余额", inline=True)
        + "</a>"
        if not covered and wallet_routes.enabled(cfg)
        else ""
    )
    return (
        '<section class="or-entry" id="openrouter">'
        '<div class="or-entry-top">'
        '<p class="or-eyebrow">OPENROUTER · /openrouter</p>'
        f'<div class="or-badges">{pricing_badge}{key_badge}</div></div>'
        "<h2>"
        + label(
            "OpenRouter models in local Claude Code",
            "在本地 Claude Code 中使用 OpenRouter 模型",
            inline=True,
        )
        + "</h2><p>"
        + label(
            "Use offered OpenRouter models directly from your computer with a private Fleet "
            "key—no slot or Claude subscription required. CLI 0.2.25 launches your last used "
            "model and puts your 10 recent models first in /model.",
            "使用个人 Fleet 密钥在本机直接调用可选 OpenRouter 模型，无需槽位或 Claude 订阅。"
            "CLI 0.2.25 启动时默认沿用上次使用的模型，并在 /model 中优先显示最近使用的 10 个模型。",
            inline=True,
        )
        + '</p><div class="or-entry-actions">'
        '<a class="btn" href="' + PAGE + '">'
        + label("OpenRouter models & usage", "OpenRouter 模型与用量", inline=True)
        + f"</a>{topup}</div></section>"
    )


def _signed_out_page(cfg: Any) -> usersite.Outcome:
    api_base = (getattr(cfg, "public_url", "") or "https://ccfleet.io").rstrip("/") + "/openrouter"
    code_base = f"<code>{escape(api_base)}</code>"
    body = (
        f'<div class="card door">{usersite.MARK}<h1>'
        + label("Sign in for OpenRouter", "登录以使用 OpenRouter", inline=True)
        + "</h1><p>"
        + bi(
            "Use local Claude Code with offered OpenRouter models through your own private "
            f"Fleet credential at {code_base}—no slot or Claude subscription required. "
            "Regular accounts pay OpenRouter’s actual usage cost plus a 5% service fee from "
            "their Fleet balance. Fleet’s upstream OpenRouter credential stays server-side.",
            f"通过你自己的个人 Fleet 密钥在本地 Claude Code 中使用可选 OpenRouter 模型，接口地址为 "
            f"{code_base}，无需槽位或 Claude 订阅。普通账户按 OpenRouter 实际用量费加 5% 服务费"
            "从 Fleet 余额扣款，Fleet 的上游 OpenRouter 凭证始终保存在服务器端。",
            inline=True,
        )
        + '</p><div class="sign-in-action">'
        '<a class="btn primary big" href="/auth/google/start?next=/account/openrouter">'
        + label("Continue with Google", "使用 Google 登录", inline=True)
        + '</a></div><p class="muted small">'
        '<a href="/docs/library/docs/public-gateway.html#openrouter">'
        + label("Read how OpenRouter works on Fleet", "查看 Fleet OpenRouter 使用说明", inline=True)
        + "</a></p></div>"
    )
    return usersite.Outcome(200, body=usersite._shell("OpenRouter", body, door=True))


def _model_rows(ordered_models: list[str], recent_set: set[str],
                models: Mapping[str, tuple[Decimal, Decimal]]) -> str:
    rows = []
    for idx, slug in enumerate(ordered_models):
        prompt, completion = models[slug]
        tags = ""
        if idx == 0:
            if slug in recent_set:
                tags += (
                    '<span class="or-tag default">'
                    + label("Last used · Default", "上次使用 · 默认", inline=True)
                    + "</span>"
                )
            else:
                tags += (
                    '<span class="or-tag default">'
                    + label("Default", "默认", inline=True)
                    + "</span>"
                )
        elif slug in recent_set:
            tags += (
                '<span class="or-tag recent">'
                + label("Recent", "最近使用", inline=True)
                + "</span>"
            )
        if prompt == 0 and completion == 0:
            ceiling = label("Free ($0)", "免费 ($0)", inline=True)
        else:
            ceiling = (
                label("In ≤ ", "输入 ≤ ", inline=True)
                + _rate_usd(prompt)
                + " · "
                + label("Out ≤ ", "输出 ≤ ", inline=True)
                + _rate_usd(completion)
            )
        rows.append(
            '<tr><td><div class="or-model-cell"><code>'
            + escape(slug)
            + f"</code>{tags}</div></td>"
            + f'<td class="num">{ceiling}</td></tr>'
        )
    return (
        '<div class="or-sheet"><table class="or-table"><thead><tr><th scope="col">'
        + label("Model ID", "模型 ID", inline=True)
        + '</th><th scope="col" class="num">'
        + label("Provider ceiling / 1M tokens", "服务商上限 / 1M tokens", inline=True)
        + "</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _receipt_rows(receipts: list[dict[str, Any]]) -> str:
    rows = []
    for row in receipts:
        cost = row["cost_usd"]
        state = row["state"]
        if cost is None:
            if state == "released":
                badge = (
                    '<span class="pill disabled">'
                    + label("Released", "已解除预留", inline=True)
                    + "</span>"
                )
                billing = (
                    '<div class="or-billing-detail"><strong>$0.00</strong><small>'
                    + label("Hold released", "预留已退回", inline=True)
                    + "</small></div>"
                )
            else:
                badge = (
                    '<span class="pill warn">'
                    + label("Reserved", "已预留", inline=True)
                    + "</span>"
                )
                if row["wallet_exempt"]:
                    billing = (
                        '<div class="or-billing-detail"><strong>$0.00</strong><small>'
                        + label("Admin covered · pending", "管理员覆盖 · 结算中", inline=True)
                        + "</small></div>"
                    )
                else:
                    hold_txt = wallet.display(row["hold_cents"])
                    billing = (
                        '<div class="or-billing-detail"><strong>'
                        + label("Reserved ", "已预留 ", inline=True)
                        + hold_txt
                        + "</strong><small>"
                        + label("Awaiting final cost", "等待最终费用结算", inline=True)
                        + "</small></div>"
                    )
        elif row["wallet_exempt"]:
            cost_fmt = _usd(Decimal(cost))
            badge = (
                '<span class="pill ok">'
                + label("Settled", "已结算", inline=True)
                + "</span>"
            )
            billing = (
                '<div class="or-billing-detail"><strong>'
                + label("Fleet wallet charge", "Fleet 余额扣费", inline=True)
                + " $0.00</strong><small>"
                + label("OpenRouter usage ", "OpenRouter 用量费 ", inline=True)
                + cost_fmt
                + " · "
                + label("Admin covered", "管理员覆盖", inline=True)
                + "</small></div>"
            )
        else:
            cost_dec = Decimal(cost)
            fee_dec = Decimal(row["fee_usd"])
            total_dec = cost_dec + fee_dec
            badge = (
                '<span class="pill ok">'
                + label("Settled", "已结算", inline=True)
                + "</span>"
            )
            billing = (
                '<div class="or-billing-detail"><strong>'
                + _usd(total_dec)
                + "</strong><small>"
                + label("Usage ", "用量费 ", inline=True)
                + _usd(cost_dec)
                + " + "
                + label("5% fee ", "5% 服务费 ", inline=True)
                + _usd(fee_dec)
                + "</small></div>"
            )
        rows.append(
            "<tr><td>"
            + _utc_time(row.get("created_at"))
            + "</td><td><code>"
            + escape(row["model"])
            + '</code><span class="or-req-id">'
            + label("Request ", "请求 ", inline=True)
            + escape(row["id"])
            + "</span></td><td>"
            + badge
            + '</td><td class="num">'
            + billing
            + "</td></tr>"
        )
    return (
        '<div class="or-sheet"><table class="or-table"><thead><tr><th scope="col">'
        + label("Time", "时间", inline=True)
        + '</th><th scope="col">'
        + label("Model & request", "模型与请求", inline=True)
        + '</th><th scope="col">'
        + label("Status", "状态", inline=True)
        + '</th><th scope="col" class="num">'
        + label("Charge", "费用", inline=True)
        + "</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def page(service: Any, cfg: Any, account: Optional[Mapping[str, Any]],
         session_id: str, *, issued: Optional[str] = None) -> usersite.Outcome:
    if not service.models:
        return usersite.not_found()
    if account is None:
        return _signed_out_page(cfg)

    owner = account["id"]
    csrf = escape(usersite.csrf_for(session_id, cfg.cookie_secret), quote=True)
    covered = service.admin_covered(account)
    has_key, carry = _account_state(service.store, owner)
    if issued:
        has_key = True

    balance_cents = service.store.wallet_balance(owner)
    balance = wallet.display(balance_cents)
    receipts = service.store.openrouter_receipts(owner)
    recent_models = service.store.openrouter_recent_models(owner, service.models)
    recent_set = set(recent_models)
    ordered_models = list(dict.fromkeys([*recent_models, *service.models]))
    default_model = ordered_models[0]
    example_model = ordered_models[1] if len(ordered_models) > 1 else ordered_models[0]
    api_base = (getattr(cfg, "public_url", "") or "https://ccfleet.io").rstrip("/") + "/openrouter"
    has_paid_models = any(p > 0 or c > 0 for p, c in service.models.values())

    settled_count = sum(1 for r in receipts if r["state"] == "settled")
    upstream_total = sum(
        (Decimal(r["cost_usd"]) for r in receipts if r["cost_usd"] is not None),
        Decimal(0),
    )
    wallet_debited_cents = sum(
        int(r.get("debit_cents") or 0) for r in receipts
        if r["state"] == "settled" and not r["wallet_exempt"]
    )
    pending_hold_cents = sum(
        int(r.get("hold_cents") or 0) for r in receipts
        if r["state"] in ("held", "submitted") and not r["wallet_exempt"]
    )

    def _action_form(action: str, en: str, zh: str, btn_cls: str) -> str:
        return (
            f'<form method="post" action="{PAGE}/{action}">'
            f'<input type="hidden" name="csrf" value="{csrf}">'
            f'<button class="btn {btn_cls}" type="submit">'
            + label(en, zh, inline=True)
            + "</button></form>"
        )

    nav = usersite._account_nav(
        PAGE,
        monthly=usersite._monthly_management(service.store, cfg, account),
        openrouter=True,
    )

    # One-time credential reveal banner (key appears EXACTLY ONCE in the HTML body).
    issued_banner = ""
    if issued:
        issued_banner = (
            '<section class="or-issued" role="status" aria-live="polite">'
            '<div class="or-issued-head">'
            '<span class="pill ok">'
            + label("Shown once", "仅显示一次", inline=True)
            + "</span><h2>"
            + label("Save your private Fleet key now", "请立即保存你的个人 Fleet 密钥", inline=True)
            + "</h2></div><p>"
            + label(
                "This key is only shown on this non-cached response. Refreshing or leaving this "
                "page hides it permanently. Any previous OpenRouter key for this account has "
                "already been replaced.",
                "此密钥仅在本次不缓存的页面响应中显示一次。刷新或离开此页面后将无法再次查看。"
                "此账户先前的 OpenRouter 密钥已立即失效。",
                inline=True,
            )
            + "</p>"
            + _code_box(
                "Private Fleet OpenRouter key · Shown once",
                "个人 Fleet OpenRouter 密钥 · 仅显示一次",
                issued,
                copy_en="Copy key",
                copy_zh="复制密钥",
            )
            + '<p class="or-note">'
            + label(
                "Next: run ccfleet openrouter in your project directory and enter this key at "
                "the hidden prompt, or store it in your secret manager as "
                "CCFLEET_OPENROUTER_TOKEN.",
                "下一步：在项目目录运行 ccfleet openrouter 并在隐藏提示中输入此密钥，"
                "或通过密钥管理器保存为 CCFLEET_OPENROUTER_TOKEN。",
                inline=True,
            )
            + "</p></section>"
        )

    # Credential management card
    if issued:
        key_pill = (
            '<span class="pill ok">'
            + label("New key issued", "已生成新密钥", inline=True)
            + "</span>"
        )
    elif has_key is True:
        key_pill = (
            '<span class="pill assigned">'
            + label("Active key on file", "密钥已启用", inline=True)
            + "</span>"
        )
    elif has_key is False:
        key_pill = (
            '<span class="pill disabled">'
            + label("No active key", "尚无有效密钥", inline=True)
            + "</span>"
        )
    else:
        key_pill = ""

    create_en = (
        "Rotate key (replace previous)" if has_key else "Issue a private Fleet key"
    )
    create_zh = "更换密钥（替换旧密钥）" if has_key else "生成个人 Fleet 密钥"
    key_hint = (
        '<p class="or-note">'
        + label(
            "Fleet stores only a SHA-256 hash of your key. If you misplaced it, rotate to "
            "issue a replacement key immediately.",
            "Fleet 仅保存密钥的 SHA-256 哈希。如遗失，请直接更换以立即生成新密钥。",
            inline=True,
        )
        + "</p>"
        if has_key and not issued
        else ""
    )

    code_base = f"<code>{escape(api_base)}</code>"
    credential_card = (
        '<section class="or-card" id="openrouter-key">'
        '<div class="or-card-head"><h2>'
        + label("Private Fleet credential", "个人 Fleet 密钥", inline=True)
        + f"</h2>{key_pill}</div>"
        '<p class="or-card-sub">'
        + bi(
            f"Each signed-in user gets a private credential scoped strictly to {code_base}. "
            "Fleet’s upstream OpenRouter API key remains server-side and is never sent to "
            "your browser or computer. This key cannot access Claude slots.",
            f"每位登录用户都有独立的个人密钥，仅限访问 {code_base}。"
            "Fleet 的上游 OpenRouter 凭证始终保留在服务器端，绝不会发送到你的浏览器或电脑。"
            "此密钥无法访问 Claude 槽位。",
            inline=True,
        )
        + '</p><div class="or-key-actions">'
        + _action_form("create", create_en, create_zh, "primary")
        + _action_form("revoke", "Revoke access", "撤销密钥", "or-btn-revoke")
        + f"</div>{key_hint}</section>"
    )

    # CLI 0.2.25 Quick Start card
    cli_card = (
        '<section class="or-card" id="openrouter-cli">'
        '<div class="or-card-head"><h2>'
        + label("Run in local Claude Code", "在本机 Claude Code 中运行", inline=True)
        + '</h2><span class="or-tag">CLI 0.2.25+</span></div>'
        '<p class="or-card-sub">'
        + label(
            "No slot or Claude subscription needed. Commands never include your key—paste it at "
            "the hidden prompt or supply CCFLEET_OPENROUTER_TOKEN from your secret manager.",
            "无需槽位或 Claude 订阅。命令中不包含密钥——请在隐藏提示中粘贴，或通过密钥管理器设置 "
            "CCFLEET_OPENROUTER_TOKEN。",
            inline=True,
        )
        + '</p><ol class="or-steps">'
        '<li class="or-step"><h3>'
        + label("Update to CC Fleet CLI 0.2.25", "升级到 CC Fleet CLI 0.2.25", inline=True)
        + "</h3>"
        + _code_box("Terminal · Upgrade CLI", "终端 · 升级 CLI", "ccfleet update")
        + '</li><li class="or-step"><h3>'
        + label(
            "Verify your catalog and pricing (no model charge)",
            "查看可用模型与计费方式（不产生模型费用）",
            inline=True,
        )
        + "</h3>"
        + _code_box("Terminal · Check catalog", "终端 · 查看模型列表", "ccfleet openrouter --check")
        + '</li><li class="or-step"><h3>'
        + label("Start Claude Code in your project", "在项目目录启动 Claude Code", inline=True)
        + "</h3>"
        + _code_box("Terminal · Launch", "终端 · 启动", "ccfleet openrouter")
        + '<div class="or-callout">'
        + bi(
            f"<strong>CLI 0.2.25 model memory:</strong> <code>ccfleet openrouter</code> "
            f"automatically starts your <strong>last used model</strong> "
            f"(next default: <code>{escape(default_model)}</code>). Inside Claude Code, "
            f"type <code>/model</code> to switch—your <strong>10 most recently used "
            f"models</strong> appear first.",
            f"<strong>CLI 0.2.25 模型记忆：</strong><code>ccfleet openrouter</code> 会自动启动你"
            f"<strong>上次使用的模型</strong>（下次默认：<code>{escape(default_model)}</code>）。"
            f"在对话中输入 <code>/model</code> 切换模型时，你<strong>最近使用的 10 个模型</strong>"
            f"会排在最前面。",
            inline=True,
        )
        + "</div><p>"
        + label(
            "Or launch a specific offered model directly:",
            "也可以在启动时直接指定某个可用模型：",
            inline=True,
        )
        + "</p>"
        + _code_box(
            "Terminal · Pick a model explicitly",
            "终端 · 指定模型启动",
            f"ccfleet openrouter --model {example_model}",
        )
        + "</li></ol>"
        '<p class="or-note">'
        + label(
            "Use ccfleet claude for your private Claude subscription slots. OpenRouter supports "
            "text and local Claude Code tools; choose models with tool-calling support.",
            "自有 Claude 订阅槽位请继续使用 ccfleet claude。OpenRouter 支持文本与本地 "
            "Claude Code 工具，请选择兼容工具调用的模型。",
            inline=True,
        )
        + "</p></section>"
    )

    # Offered models & recent-10 catalog card
    visible_models = ordered_models[:10]
    overflow_models = ordered_models[10:]
    recent_summary = (
        label(
            f"{len(ordered_models)} offered · {len(recent_models)} recent",
            f"共 {len(ordered_models)} 个模型 · {len(recent_models)} 个最近使用",
            inline=True,
        )
        if recent_models
        else label(
            f"{len(ordered_models)} offered",
            f"共 {len(ordered_models)} 个可用模型",
            inline=True,
        )
    )
    overflow_html = ""
    if overflow_models:
        overflow_html = (
            '<details class="or-more-models"><summary>'
            + label(
                f"Show {len(overflow_models)} more offered models",
                f"展开其余 {len(overflow_models)} 个可用模型",
                inline=True,
            )
            + "</summary>"
            + _model_rows(overflow_models, recent_set, service.models)
            + "</details>"
        )
    models_card = (
        '<section class="or-card" id="openrouter-models">'
        '<div class="or-card-head"><h2>'
        + label("Offered models", "可用模型", inline=True)
        + f'</h2><span class="or-tag">{recent_summary}</span></div>'
        '<p class="or-card-sub">'
        + label(
            "Ordered exactly like /model in CLI 0.2.25: up to 10 recently used models appear "
            "first, followed by the remaining catalog. Ceilings cap upstream provider pricing; "
            "actual billing uses OpenRouter’s reported generation cost.",
            "排序与 CLI 0.2.25 的 /model 完全一致：最近使用的最多 10 个模型排在最前，"
            "其余模型按目录顺序排列。价格上限用于限制上游服务商最高单价；实际按 OpenRouter "
            "报告的生成费用结算。",
            inline=True,
        )
        + "</p>"
        + _model_rows(visible_models, recent_set, service.models)
        + overflow_html
        + "</section>"
    )

    # Usage receipts card
    if receipts:
        if covered:
            charged_stat = "$0.00"
            charged_lbl = label("Fleet wallet charge", "Fleet 余额扣费", inline=True)
        else:
            charged_stat = wallet.display(wallet_debited_cents)
            charged_lbl = label("Fleet wallet debited", "Fleet 余额实扣", inline=True)
        receipts_inner = (
            '<div class="or-stats">'
            '<div class="or-stat"><span class="or-stat-label">'
            + label("Settled requests", "已结算请求", inline=True)
            + f'</span><strong class="or-stat-value">{settled_count}</strong></div>'
            '<div class="or-stat"><span class="or-stat-label">'
            + label("OpenRouter usage", "OpenRouter 用量", inline=True)
            + f'</span><strong class="or-stat-value">{_usd(upstream_total)}</strong></div>'
            '<div class="or-stat"><span class="or-stat-label">'
            + charged_lbl
            + f'</span><strong class="or-stat-value">{charged_stat}</strong></div>'
            "</div>"
            + _receipt_rows(receipts)
        )
    else:
        receipts_inner = (
            '<div class="or-empty"><strong>'
            + label("No OpenRouter requests yet", "暂无 OpenRouter 请求记录", inline=True)
            + "</strong><p>"
            + label(
                "Issue your private Fleet key above and run ccfleet openrouter in a project "
                "to see live reservations and settled generation receipts here.",
                "在上方生成个人 Fleet 密钥，并在项目目录运行 ccfleet openrouter 后，"
                "此处将显示预留与已结算的用量明细。",
                inline=True,
            )
            + "</p></div>"
        )

    receipts_card = (
        '<section class="or-card" id="openrouter-receipts">'
        '<div class="or-card-head"><h2>'
        + label("Recent usage", "最近用量", inline=True)
        + '</h2><span class="or-note">'
        + label("Latest 50 requests", "最近 50 条记录", inline=True)
        + "</span></div>"
        + receipts_inner
        + "</section>"
    )

    # Right sticky rail: Account-specific pricing & Fleet credit summary
    if covered:
        billing_card = (
            '<section class="or-balance-card admin-covered" id="openrouter-pricing">'
            '<p class="or-eyebrow">'
            + label("Account pricing · Admin covered", "账户计费 · 管理员覆盖", inline=True)
            + '</p><p class="or-figure">$0.00</p>'
            '<p class="or-figure-sub">'
            + label(
                "Fleet wallet charge for OpenRouter",
                "OpenRouter 的 Fleet 余额扣费",
                inline=True,
            )
            + '</p><p class="or-card-sub">'
            + label(
                "Fleet covers this admin account’s OpenRouter usage. Requests use the same "
                "upstream OpenRouter service with no Fleet wallet charge or credit hold.",
                "Fleet 承担此管理员账户的 OpenRouter 用量费用。请求使用相同的 OpenRouter "
                "上游服务，不从 Fleet 余额扣费，也无需预留额度。",
                inline=True,
            )
            + '</p><dl class="or-meta">'
            "<div><dt>"
            + label("Pricing tier", "计费方式", inline=True)
            + '</dt><dd><span class="or-tag covered">'
            + label("Admin covered · $0.00", "管理员覆盖 · $0.00", inline=True)
            + "</span></dd></div>"
            "<div><dt>"
            + label("API base", "API 地址", inline=True)
            + "</dt><dd><code>/openrouter</code></dd></div>"
            "<div><dt>"
            + label("Upstream credential", "上游凭证", inline=True)
            + "</dt><dd>"
            + label("Server-side only", "仅限服务器端", inline=True)
            + "</dd></div>"
            "<div><dt>"
            + label("Fleet credit (uncharged)", "Fleet 余额（不扣费）", inline=True)
            + f"</dt><dd>{balance}</dd></div>"
            "</dl></section>"
        )
    else:
        hold_note = (
            '<p class="or-hold-note">'
            + label(
                f"{wallet.display(pending_hold_cents)} is currently reserved for in-flight "
                "OpenRouter requests and already excluded from the available balance above.",
                f"当前有 {wallet.display(pending_hold_cents)} 正被进行中的 OpenRouter 请求预留，"
                "上述可用余额已扣除该预留金额。",
                inline=True,
            )
            + "</p>"
            if pending_hold_cents > 0
            else ""
        )
        low_note = (
            '<p class="or-note">'
            + label(
                "Add Fleet credit to start using paid OpenRouter models.",
                "充值 Fleet 余额后即可开始使用付费 OpenRouter 模型。",
                inline=True,
            )
            + "</p>"
            if balance_cents == 0 and has_paid_models
            else ""
        )
        carry_row = (
            "<div><dt>"
            + label("Carried fraction", "结转零头", inline=True)
            + f"</dt><dd>{_usd(carry)}</dd></div>"
            if carry > 0
            else ""
        )
        billing_card = (
            '<section class="or-balance-card" id="openrouter-pricing">'
            '<p class="or-eyebrow">'
            + label("Available Fleet credit", "可用 Fleet 余额", inline=True)
            + f'</p><p class="or-figure">{balance}</p>'
            '<p class="or-figure-sub">'
            + label(
                "OpenRouter usage + 5% service fee",
                "OpenRouter 用量费 + 5% 服务费",
                inline=True,
            )
            + f"</p>{hold_note}{low_note}"
            '<a class="btn primary or-topup-btn" href="/account/topup">'
            + label("Add credit", "充值余额", inline=True)
            + '</a><p class="or-card-sub">'
            + label(
                "Your Fleet balance pays the OpenRouter usage cost plus a 5% service fee "
                "(for example, $1.00 of upstream usage totals $1.05).",
                "Fleet 余额支付 OpenRouter 的实际用量费及 5% 服务费"
                "（例如 $1.00 上游用量费合计 $1.05）。",
                inline=True,
            )
            + '</p><dl class="or-meta">'
            "<div><dt>"
            + label("Rate", "费率", inline=True)
            + "</dt><dd>OpenRouter + 5%</dd></div>"
            "<div><dt>"
            + label("API base", "API 地址", inline=True)
            + "</dt><dd><code>/openrouter</code></dd></div>"
            "<div><dt>"
            + label("Upstream credential", "上游凭证", inline=True)
            + "</dt><dd>"
            + label("Server-side only", "仅限服务器端", inline=True)
            + "</dd></div>"
            "<div><dt>"
            + label("Fractional cents", "不足一美分", inline=True)
            + "</dt><dd>"
            + label("Carried forward", "自动结转", inline=True)
            + f"</dd></div>{carry_row}"
            "</dl></section>"
        )

    trust_card = (
        '<aside class="or-trust"><h3>'
        + label("Security & data boundary", "安全与数据边界", inline=True)
        + "</h3><ul><li>"
        + label(
            "Operator credential stays server-side: Fleet signs upstream OpenRouter requests on "
            "the server; your computer only uses your revocable Fleet key.",
            "运营方凭证仅存服务器端：Fleet 在服务器端向 OpenRouter 发起认证，"
            "你的电脑仅使用可随时撤销的个人 Fleet 密钥。",
            inline=True,
        )
        + "</li><li>"
        + label(
            "OpenRouter and the selected provider receive the conversation and tool results.",
            "OpenRouter 和所选服务商会收到对话和工具结果。",
            inline=True,
        )
        + "</li><li>"
        + label(
            "Tool execution, approvals, and saved files stay on your computer. Provider-hosted "
            "web tools are disabled on this route.",
            "工具执行、授权和已保存的文件都留在你的电脑上。此路由不启用服务商托管的网页工具。",
            inline=True,
        )
        + "</li></ul></aside>"
    )

    details_card = (
        '<details class="or-details"><summary>'
        + label(
            "How holds, settlement & recent models work",
            "预留、结算与最近模型说明",
            inline=True,
        )
        + '</summary><div class="or-details-body"><p>'
        + label(
            "Pre-dispatch holds: before forwarding a paid request, Fleet reserves an estimated "
            "ceiling from your available balance. When OpenRouter returns the final generation "
            "cost, Fleet settles to the exact cost plus 5% and releases any unused hold. If a "
            "stream is interrupted, Fleet reconciles the upstream generation receipt in the "
            "background.",
            "请求前预留：在转发付费请求前，Fleet 会按预估上限暂时预留余额；"
            "OpenRouter 返回最终生成费用后，按实际费用加 5% 精确结算并退回多余预留。"
            "若流式连接中断，Fleet 会在后台核对上游生成账单。",
            inline=True,
        )
        + "</p><p>"
        + label(
            "Personal model recency: only settled requests for your own Fleet account update "
            "your 10-model recent list. CLI 0.2.25 automatically starts with your most recently "
            "used offered model and shows recent models first in /model.",
            "个人最近模型：只有你自己账户的已结算请求会更新最近 10 个模型列表。CLI 0.2.25 "
            "会自动以上次使用的可用模型启动，并在 /model 中将最近模型排在最前。",
            inline=True,
        )
        + '</p><p><a href="/docs/library/docs/public-gateway.html#openrouter">'
        + label(
            "Read the full OpenRouter gateway guide",
            "阅读完整的 OpenRouter 网关说明",
            inline=True,
        )
        + "</a></p></div></details>"
    )

    body = (
        '<section class="or-page">'
        '<div class="or-head"><div>'
        '<p class="or-eyebrow">OPENROUTER GATEWAY · /openrouter</p>'
        "<h1>OpenRouter</h1>"
        '<p class="sub">'
        + label(
            "Run offered OpenRouter models in local Claude Code with your private Fleet "
            "credential—no slot required.",
            "使用个人 Fleet 密钥在本机 Claude Code 中调用可选 OpenRouter 模型，无需槽位。",
            inline=True,
        )
        + '</p></div><a class="btn quiet" href="/docs/library/docs/public-gateway.html#openrouter">'
        + label("Guide", "使用说明", inline=True)
        + "</a></div>"
        + nav
        + issued_banner
        + '<div class="or-grid">'
        + f'<div class="or-main">{credential_card}{cli_card}{models_card}{receipts_card}</div>'
        + f'<div class="or-rail">{billing_card}{trust_card}{details_card}</div>'
        + "</div></section>"
    )

    viewer = usersite.viewer_for(account, session_id, cfg, service.store)
    return usersite.Outcome(
        200,
        body=usersite._shell("OpenRouter", body, viewer=viewer, width="wide", extra_css=PAGE_CSS),
    )
