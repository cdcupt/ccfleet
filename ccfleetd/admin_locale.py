"""Authored console labels; account data, machine reports and credentials stay literal."""

from __future__ import annotations

from html import escape

from .bilingual import bi
from .bilingual import label as bilingual_label

LABELS_ZH = {
    "Fleet admin": "Fleet 管理后台",
    "Your machines": "我的机器",
    "Manage accounts, credit and slots.": "管理账户、余额与槽位。",
    "Check your machines and sign-in status.": "查看机器与登录状态。",
    "Waiting for you to paste a code": "等待您粘贴验证码",
    "Keeping up with a sign-in": "正在更新登录进度",
    "Status updates keep your open forms": "刷新状态时会保留未提交的表单",
    "No nodes yet. Use the form below.": "暂无节点，请使用下方表单添加。",
    "No nodes are assigned to you yet. Your operator adds them.": (
        "尚未为您分配节点，请联系运营方添加。"),
    "healthy": "正常",
    "warning": "警告",
    "critical": "严重",
    "disabled": "已停用",
    "Machine": "机器",
    "Providers / IP type": "供应商 / IP 类型",
    "Hosting invoice": "主机账单",
    "IP invoice": "IP 账单",
    "Resource / month": "每月资源成本",
    "Service fee / month": "每月服务费",
    "Customer / month": "客户月租",
    "Not set": "尚未设置",
    "Both recorded USD costs are required": "需要完整记录两项美元成本",
    "Unavailable": "暂无数据",
    "Host provider": "主机供应商",
    "IP provider": "IP 供应商",
    "IP type": "IP 类型",
    "Invoice amount": "账单金额",
    "Currency": "币种",
    "Billing period": "计费周期",
    "Earlier payment notes": "早期付款备注",
    "Payments": "付款记录",
    "Disable": "停用",
    "Enable": "启用",
    "RC alert off": "关闭远程控制告警",
    "RC alert on": "开启远程控制告警",
    "New token": "新建令牌",
    "Remove": "移除",
    "Add node": "添加节点",
    "Sign in": "登录",
    "Sign in again": "重新登录",
    "Send code": "提交验证码",
    "Cancel": "取消",
    "Get a device token": "获取设备令牌",
    "Show token": "显示令牌",
    "Done": "完成",
    "Take back": "收回槽位",
    "Remove the slot": "移除槽位",
    "Reservation": "预留",
    "Clear reservation": "取消预留",
    "Keep for somebody": "为用户预留",
    "Reserve": "预留",
    "Set capacity": "设置容量",
    "Declare its slot": "声明槽位",
    "Advanced": "高级设置",
    "Machine identity": "机器标识",
    "Their own machine": "用户自有机器",
    "Save slot offer": "保存槽位报价",
    "Clear slot offer": "清除槽位报价",
    "Providers, IP type & price": "供应商、IP 类型与价格",
    "Set allowance": "设置槽位上限",
    "Record payment": "记录付款",
    "Void": "作废",
    "Set price": "设置价格",
    "Clear price": "清除价格",
    "Available": "可用",
    "Not signed in": "未登录",
    "Setting up": "正在配置",
    "Ready to sign in": "等待登录",
    "Being wiped": "正在清理",
    "Sign in to Claude": "登录 Claude",
    "In use": "使用中",
    "Wiping": "正在清理",
    "Stuck": "配置停滞",
    "Needs attention": "需要处理",
    "Signed in": "已登录",
    "signed in": "已登录",
    "not signed in": "未登录",
    "unknown": "未知",
    "Devin ready": "Devin 已就绪",
    "Devin needs a check": "Devin 需要验证",
    "reserved": "已预留",
    "stuck": "配置停滞",
    "update failed": "更新失败",
    "machine disabled": "机器已停用",
    "reboot needed": "需要重启",
    "more than one slot": "存在多个槽位",
    "hostname pending": "主机名等待更新",
    "Residential": "住宅网络",
    "ISP": "ISP",
    "Datacenter": "数据中心",
    "Not verified": "尚未验证",
    "Hosting cost": "主机成本",
    "IP cost": "IP 成本",
    "Calendar month": "自然月",
    "30 days": "30 天",
    "Year": "年",
    "Latest": "最新版",
    "Stable": "稳定版",
}


def label(text: str) -> str:
    """Translate a label at an explicit UI authoring boundary, never arbitrary page text."""
    chinese = LABELS_ZH.get(text)
    return bilingual_label(text, chinese, inline=True) if chinese else escape(text)


HINTS_ZH = {
    "While it is available, only they can claim it.": "槽位空闲时，仅预留用户可以领取。",
    "Once cleared, anybody with an allowance may claim it.": (
        "取消预留后，任何有槽位额度的用户都可以领取。"),
    "Being wiped already: it is available again once the machine confirms.": (
        "正在清理，机器确认完成后才能重新使用。"),
    "Brands, network type and normalized monthly quote breakdown are public. "
    "Enter 0 only for an exit IP included in the recorded bundled hosting cost; "
    "a blank component or unconverted foreign costs leave pricing unavailable. "
    "Alpha pricing rounds resource cost, adds a rounded service fee of 10% with "
    "a $2 minimum. Changing this quote does not itself charge customers or "
    "change an existing monthly plan's confirmed rate. "
    "Save to update the displayed preview.":
    "品牌、网络类型和标准化月租明细会公开展示。"
    "只有出口 IP 已包含在主机合并成本中时才填写 0；"
    "成本留空或外币成本尚未换算为美元时，无法提供报价。Alpha 定价将资源成本四舍五入到美分，"
    "再加 10% 服务费，最低 2 美元。更改报价不会向客户扣款，也不会更改已确认的月租价格。"
    "保存后更新价格预览。",
}


def hint(text: str) -> str:
    """Pair trusted authored help text; dynamic machine/user messages keep their original text."""
    chinese = HINTS_ZH.get(text)
    return bi(text, chinese, inline=True) if chinese else text


def attributes(text: str) -> str:
    """Attach authored leaf translation while keeping the original control structure."""
    chinese = LABELS_ZH.get(text)
    return ' data-copy-zh="' + escape(chinese, quote=True) + '"' if chinese else ""
