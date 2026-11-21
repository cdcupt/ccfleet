"""Subscription compatibility for the public, localized getting-started guide."""

from html import escape

from .bilingual import bi, label

# Each row describes the named agent mode. Editors can also have a separately billed chat.
ROWS = (
    ("T3 Code",
     ("Choose the Claude provider. Fleet launcher setup is documented below.",
      "选择 Claude 服务商。下方有 Fleet 启动器的设置步骤。"),
     ("Choose Antigravity in Settings → Providers on the environment running the project. "
      "T3 manages a separate Google sign-in for the official agent.",
      "在运行项目的环境中，打开 Settings → Providers，选择 Antigravity。"
      "T3 为官方代理单独管理 Google 登录。"),
     (("Claude", "https://github.com/pingdotgg/t3code/blob/main/docs/user/providers-claude.md"),
      ("Antigravity", "https://github.com/pingdotgg/t3code/blob/main/docs/user/providers-antigravity.md"))),
    ("Pi",
     ("Pi lists an Anthropic API credential. Community subscription extensions exist. "
      "CCFleet has no verified Pi subscription setup.",
      "Pi 文档列出 Anthropic API 凭据。社区有订阅扩展，CCFleet 尚未验证 Pi 的订阅接入。"),
     ("Pi lists a Gemini API key. We found no Google-supported way for Pi’s own agent "
      "to draw on a personal Antigravity subscription.",
      "Pi 文档列出 Gemini API 密钥。尚未找到 Google 支持的、供 Pi 自身代理使用"
      "个人 Antigravity 订阅额度的方式。"),
     (("Pi", "https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/providers.md"),
      ("Google", "https://antigravity.google/docs/faq"))),
    ("Orca · Herdr",
     ("Set the Claude executable to your Fleet launcher. See the settings below.",
      "将 Claude 可执行文件设为 Fleet 启动器，具体设置见下方。"),
     ("We have no verified Antigravity subscription integration for either app.",
      "这两款应用均无经我们验证的 Antigravity 订阅接入。"),
     (("CCFleet", "#agent-apps"),)),
    ("VS Code",
     ("Install Anthropic’s Claude Code extension and sign in to Claude.",
      "安装 Anthropic 的 Claude Code 扩展并登录 Claude。"),
     ("Install Google’s Antigravity extension and sign in with your personal Google account.",
      "安装 Google 的 Antigravity 扩展并使用个人 Google 账号登录。"),
     (("Claude Code", "https://code.claude.com/docs/en/vs-code"),
      ("Antigravity", "https://antigravity.google/docs/ide/extensions/vscode"))),
    ("Zed",
     ("In the Agent Panel, open Claude Agent and choose Claude Code login where offered.",
      "在 Agent Panel 中新建 Claude Agent 会话；如提供 Claude Code 登录选项，选择该项。"),
     ("Select the official Antigravity agent in an external-agent thread and use Google sign-in.",
      "在外部代理会话中选择官方 Antigravity 代理，用 Google 账号登录。"),
     (("Zed", "https://zed.dev/docs/ai/external-agents"),
      ("Antigravity ACP", "https://antigravity.google/docs/ide/extensions/acp"))),
    ("JetBrains IDEs · Android Studio",
     ("Use Anthropic’s Claude Code plugin or run Claude Code in the IDE terminal.",
      "使用 Anthropic 的 Claude Code 插件，或在 IDE 终端运行 Claude Code。"),
     ("Add the official Antigravity agent using the editor’s Agent Client Protocol (ACP).",
      "通过编辑器的 Agent Client Protocol（ACP）集成添加官方 Antigravity 代理。"),
     (("Claude Code", "https://code.claude.com/docs/en/jetbrains"),
      ("Antigravity ACP", "https://antigravity.google/docs/ide/extensions/acp"))),
    ("Xcode",
     ("Choose Claude Agent in Xcode and sign in with an eligible Claude plan.",
      "在 Xcode 的智能设置中选择 Claude Agent，并用符合条件的 Claude 订阅登录。"),
     ("Register the official Antigravity agent through Xcode’s external-agent settings.",
      "通过 Xcode 的外部代理设置注册官方 Antigravity 代理。"),
     (("Claude", "https://www.anthropic.com/news/claude-in-xcode"),
      ("Antigravity ACP", "https://antigravity.google/docs/ide/extensions/acp"))),
    ("Visual Studio",
     ("A local terminal can run Claude Code. This is separate from Visual Studio’s Copilot chat.",
      "可在本地终端运行 Claude Code。Visual Studio 自带的 Copilot 聊天使用独立账号。"),
     ("Use Google’s Antigravity extension for Visual Studio on supported Windows versions.",
      "在受支持的 Windows 版本上安装 Google 的 Visual Studio 专用 Antigravity 扩展。"),
     (("Antigravity", "https://antigravity.google/docs/ide/extensions/visual-studio"),)),
    ("Cursor",
     ("Anthropic offers its Claude Code extension for Cursor. Cursor Agent has separate billing.",
      "Anthropic 为 Cursor 提供 Claude Code 扩展。Cursor Agent 单独计费。"),
     ("Cursor’s Gemini model uses Cursor billing or an API key. For personal quota, "
      "open the project with Antigravity CLI in a terminal.",
      "Cursor 的 Gemini 模型通过 Cursor 或 API 密钥计费。使用个人订阅时，"
      "可在终端中用 Antigravity CLI 打开项目。"),
     (("Claude Code", "https://code.claude.com/docs/en/vs-code"),
      ("Cursor", "https://cursor.com/docs/models-and-pricing"))),
    ("Cline",
     ("Choose the Claude Code provider and authenticate the local Claude CLI with Pro or Max.",
      "选择 Claude Code 服务商，用 Pro 或 Max 订阅登录本地 Claude CLI。"),
     ("Cline’s Google provider uses Gemini API credentials. Personal Antigravity quota "
      "in Cline’s own agent is unverified.",
      "Cline 的 Google 服务商使用 Gemini API 凭据。尚未验证可使用个人 Antigravity 额度。"),
     (("Claude Code", "https://docs.cline.bot/provider-config/anthropic"),
      ("Google", "https://docs.cline.bot/provider-config/google-gemini"))),
    ("goose · OpenHands",
     ("goose documents claude-acp. In OpenHands Agent Canvas, choose Claude Code and "
      "authenticate on the backend that runs the project.",
      "goose 提供 claude-acp。在 OpenHands Agent Canvas 中选择 Claude Code，"
      "并在实际运行项目的后端登录。"),
     ("Their legacy Gemini CLI instructions do not establish personal Antigravity access. "
      "goose directs Gemini users to its Google API or Vertex provider.",
      "旧版 Gemini CLI 的说明不能作为使用个人 Antigravity 额度的依据。"
      "goose 目前指引 Gemini 用户使用 Google API 或 Vertex。"),
     (("goose", "https://github.com/aaif-goose/goose/blob/main/documentation/docs/guides/cli-providers.md"),
      ("OpenHands", "https://docs.openhands.dev/openhands/usage/llms/llms"))),
    ("Conductor · Paradev (Superset)",
     ("Conductor supports Claude Code with an existing Pro or Max account. Paradev can "
      "run the official CLI inside a project worktree.",
      "Conductor 支持 Claude Pro 或 Max 登录。Paradev 可在项目工作树中运行官方 CLI。"),
     ("Paradev lists Antigravity CLI as a supported terminal agent. In Conductor, "
      "we found no documented Antigravity subscription integration.",
      "Paradev 列出对 Antigravity CLI 终端代理的支持。尚未找到 Conductor 官方订阅接入说明。"),
     (("Conductor", "https://www.conductor.build/docs/reference/harnesses/claude-code"),
      ("Paradev", "https://docs.paradev.com/supported-agents"))),
    ("Hermes Agent",
     ("Hermes flags extra usage charges for its Anthropic OAuth option. Do not count "
      "that option as included Claude quota.",
      "Hermes 提醒其 Anthropic OAuth 选项使用额外付费额度，不能按 Claude 套餐内额度计算。"),
     ("Hermes documents Google API or Vertex billing and lists no personal Gemini plan route.",
      "Hermes 的 Google 接口使用 API 密钥或 Vertex 计费，未列出个人 Gemini 订阅接入。"),
     (("Hermes", "https://github.com/NousResearch/hermes-agent/blob/main/website/docs/integrations/providers.md"),)),
    ("OpenClaw",
     ("Its Claude CLI backend uses the CLI’s login. Fleet compatibility is unverified.",
      "其 Claude CLI 后端可使用 CLI 已有的登录。Fleet 兼容性尚未验证。"),
     ("Google lists OpenClaw among third-party agents restricted from using an Antigravity login.",
      "Google 在限制第三方代理使用 Antigravity 登录的说明中明确列出 OpenClaw。"),
     (("OpenClaw", "https://docs.openclaw.ai/providers/anthropic"),
      ("Google", "https://antigravity.google/docs/faq"))),
    ("OpenCode · Kilo Code",
     ("Check the selected provider’s billing. Kilo’s Claude linked billing requires a Claude "
      "API organization. Neither is a verified Fleet Claude subscription client.",
      "请查看所选服务商的计费说明。Kilo 的 Claude 关联计费要求 Claude API 组织账号。"
      "两者均未经 Fleet 验证可接入 Claude 订阅。"),
     ("Google lists OpenCode in its login restrictions. Kilo documents Gemini API keys.",
      "Google 在 Antigravity 登录限制中明确列出 OpenCode。Kilo 提供 Gemini API 接入。"),
     (("OpenCode", "https://opencode.ai/docs/providers/"),
      ("Kilo · Claude", "https://kilo.ai/docs/ai-providers/claude-linked-billing"),
      ("Kilo · Gemini", "https://kilo.ai/docs/ai-providers/gemini"))),
    ("Aider · Continue · Crush",
     ("The documented setup uses provider credentials. We found no verified Fleet "
      "subscription setup for these agents.",
      "这些工具在模型设置中使用服务商凭据。尚未验证它们可通过 Fleet 使用订阅。"),
     ("Their model integrations use provider endpoints. Selecting Gemini there does "
      "not establish use of an Antigravity subscription.",
      "这些工具连接服务商接口。在其中选择 Gemini 并不代表使用 Antigravity 订阅。"),
     (("Aider", "https://aider.chat/docs/llms.html"), ("Continue", "https://docs.continue.dev/customize/models"),
      ("Crush", "https://github.com/charmbracelet/crush"))),
    ("GitHub Copilot · Windsurf / Devin Desktop",
     ("Claude in these products’ own model pickers uses the product’s plan or billing.",
      "在这些产品自带的模型选择器中，Claude 使用对应产品的套餐或计费方式。"),
     ("Their Gemini options also follow the product’s own billing. Check the plan and "
      "model availability for your account.",
      "Gemini 选项也按产品自身规则计费，可用模型取决于你的账号和套餐。"),
     (("GitHub Copilot", "https://docs.github.com/en/copilot/reference/ai-models/supported-models"),
      ("Devin Desktop", "https://docs.devin.ai/desktop/models"))),
    ("Warp",
     ("Run Claude Code interactively in a local Warp terminal with your Claude login. "
      "Warp cloud agents have their own credential settings.",
      "在 Warp 本地终端运行 Claude Code 并登录 Claude。Warp 云端代理另行配置凭据。"),
     ("Run Antigravity CLI directly in the local terminal for your personal quota. "
      "Warp Agent has separate provider settings.",
      "在本地终端直接运行 Antigravity CLI 即可使用个人额度。Warp Agent 另有服务商设置。"),
     (("Warp", "https://docs.warp.dev/agents/getting-started/faqs"),)),
    ("Amp · Factory Droid",
     ("Amp advertises linked Claude subscriptions. Factory documents its own plans and BYOK. "
      "CCFleet has verified neither integration.",
      "Amp 列出 Claude 订阅接入，Factory 提供自身套餐和自带密钥方案。"
      "CCFleet 尚未验证这两种集成。"),
     ("We found no documented personal Antigravity subscription route for either agent.",
      "尚未找到这两款代理使用个人 Antigravity 订阅的官方接入说明。"),
     (("Amp", "https://ampcode.com/news/free-agent"), ("Factory", "https://docs.factory.ai/pricing/individuals"))),
    ("Codex",
     ("Codex uses its own authentication. No Fleet Claude subscription integration is verified.",
      "Codex 使用自身的认证方式。尚未验证可通过 Fleet 接入 Claude 订阅。"),
     ("We found no supported way for the Codex agent to use a personal Antigravity subscription.",
      "尚未找到受支持的、供 Codex 代理使用个人 Antigravity 订阅的方式。"),
     (("Codex", "https://developers.openai.com/codex/auth"), ("Google", "https://antigravity.google/docs/faq"))),
)


def _row(name, claude, gemini, sources):
    links = ' · '.join(f'<a href="{escape(url, quote=True)}">{escape(title)}</a>'
                       for title, url in sources)
    return ('<tr><th scope="row">' + escape(name) + '</th><td>'
            + label(*claude, inline=True)
            + '</td><td>' + label(*gemini, inline=True)
            + '</td><td>' + links + '</td></tr>')


def section() -> str:
    columns = (("Tool", "工具"), ("Claude subscription", "Claude 订阅"),
               ("Antigravity subscription", "Antigravity 订阅"), ("Sources", "资料"))
    return (
        '<section class="card" id="agent-compatibility" '
        'aria-labelledby="agent-compatibility-title">'
        '<h2 id="agent-compatibility-title">'
        + label("Which coding tools can use my subscription?", "哪些编程工具可以使用我的订阅？",
                inline=True)
        + '</h2>'
        + bi('<p><strong>Claude on CCFleet today.</strong> Open your project on the computer '
             'where you want edits and tests to run. Local Claude Code sends model requests '
             'through CCFleet to the assigned Claude slot. Orca, Herdr and T3 have '
             '<a href="#agent-apps">Fleet launcher instructions</a>. Launcher checks passed. '
             'Full app sessions have not been verified for all three.</p>',
             '<p><strong>通过 CCFleet 使用 Claude</strong>在需要修改文件和运行测试的电脑上'
             '打开项目。本地 Claude Code 通过 CCFleet 使用分配给你的 Claude 槽位。'
             'Orca、Herdr 和 T3 有<a href="#agent-apps">Fleet 启动器设置说明</a>。'
             '启动器检查已通过，三款应用的完整会话尚未逐一验证。</p>')
        + bi('<p><strong>Native terminal commands.</strong> In your project, run '
             '<code>ccfleet claude</code> for Claude Code. On macOS, '
             '<code>ccfleet gemini --preview</code> starts local Antigravity and verifies '
             'a native file read and edit in a disposable subfolder through your private slot. '
             'The slot supplies synthetic responses. Live subscription inference is unavailable. '
             'Each launch checks the selected agent’s latest official release.</p>',
             '<p><strong>本地终端命令。</strong>在项目中用 <code>ccfleet claude</code> 启动 '
             'Claude Code。macOS 上的 <code>ccfleet gemini --preview</code> 会启动本地 '
             'Antigravity，通过专属槽位在临时子目录中验证原生文件读写。槽位返回模拟响应，'
             '暂未开放订阅模型推理。每次启动都会检查所选代理的最新官方版本。</p>')
        + bi('<p><strong>Gemini with an Antigravity subscription today.</strong> '
             'Open your project in Ghostty or another terminal and start Antigravity CLI there. '
             'You can also choose its official '
             'agent in a supported editor below. On an enabled private Mac slot, use '
             '<a href="/account/gemini#subscription">CCFleet Workspace in Ghostty</a> to keep '
             'the Google login on the slot and access a selected MacBook project. Workspace tools '
             'ask before saving files or running a chosen test locally. The CLI’s built-in tools '
             'run on the slot. Gemini API billing is separate.</p>',
             '<p><strong>使用 Antigravity 订阅</strong>在本机 Ghostty 或其他终端打开项目，'
             '启动 Antigravity CLI，也可选择下表中的官方代理。已启用工作区的专属 Mac 槽位可通过 '
             '<a href="/account/gemini#subscription">Ghostty 中的 CCFleet Workspace</a> '
             '使用 MacBook 上指定的项目。Google 登录保留在槽位。工作区工具在本机保存文件或运行'
             '所选测试前请求确认。CLI 内置工具在槽位上运行。Gemini API 单独计费。</p>')
        + bi('<p>Checked November 21, 2026 against publisher documentation. The table covers '
             'widely used editors, command-line agents and Fleet launcher apps. Each cell '
             'describes the named mode. Fleet checks cover the three launchers above.</p>',
             '<p>2026 年 11 月 21 日核对了各方官方文档。下表收录常见编辑器、命令行代理和支持 '
             'Fleet 启动器的应用。Fleet 检查仅涵盖上文三款应用的启动器。</p>')
        + '<div class="doc-table-scroll" role="region" tabindex="0" '
        'aria-labelledby="agent-compatibility-title">'
        '<table><thead><tr>' + ''.join('<th scope="col">' + label(*titles, inline=True) + '</th>'
                                       for titles in columns)
        + '</tr></thead><tbody>' + ''.join(_row(*row) for row in ROWS) + '</tbody></table></div>'
        + bi('<p>For a personal Google plan, pick the official Antigravity agent. Google '
             'retired personal sign-in in the older Gemini CLI in June 2026. A community '
             'plug-in or a matching model name alone does not establish subscription access. '
             '<a href="https://antigravity.google/docs/faq">Google FAQ</a> · '
             '<a href="https://developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals">'
             'Gemini CLI deprecation notice</a> · <a href="https://code.claude.com/docs/en/legal-and-compliance">'
             'Anthropic authentication policy</a></p>',
             '<p>使用个人 Google 订阅时，请选择官方 Antigravity 代理。Google 已于 2026 年 6 月'
             '停止旧版 Gemini CLI 的个人登录。仅凭社区插件或同名模型无法确认是否使用订阅额度。'
             '<a href="https://antigravity.google/docs/faq">Google 常见问题</a> · '
             '<a href="https://developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals">'
             'Gemini CLI 停用公告</a> · <a href="https://code.claude.com/docs/en/legal-and-compliance">'
             'Anthropic 认证政策</a></p>') + '</section>'
    )
