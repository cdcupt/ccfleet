"""Subscription compatibility for the public, localized getting-started guide."""

from html import escape

from .bilingual import bi, label

# Each row describes the named agent mode. Editors can also have a separately billed chat.
ROWS = (
    ("T3 Code",
     ("Choose the Claude provider. Fleet launcher setup is documented below.",
      "选择 Claude 服务商。下方有 Fleet 启动器的设置步骤。"),
     ("On your Mac, pair with CCFleet and choose Local ACP command in T3. "
      "The Antigravity agent uses the Google sign-in on your private slot.",
      "在 Mac 上与 CCFleet 配对，在 T3 中选择 Local ACP command。"
      "Antigravity 代理使用专属槽位上的 Google 登录。"),
     (("CCFleet", "#t3-gemini"),
      ("T3 ACP", "https://github.com/pingdotgg/t3code/blob/main/docs/user/providers-acp.md"),
      ("Claude", "https://github.com/pingdotgg/t3code/blob/main/docs/user/providers-claude.md"))),
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
     ("Run ccfleet claude in a local project terminal, or use a fixed-slot launcher.",
      "在本地项目终端运行 ccfleet claude，也可使用固定槽位启动器。"),
     ("Run ccfleet gemini using an Orca Quick Command or a Herdr desktop program. "
      "This opens the CCFleet project terminal and uses the Google sign-in on your slot.",
      "通过 Orca 的 Quick Command 或 Herdr 桌面版的程序条目运行 ccfleet gemini，"
      "即可在项目终端使用槽位上的 Google 登录。"),
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


def t3_provider_steps() -> str:
    """The same local provider setup appears in the public guide and the account receipt."""
    menu = "<strong>Settings → Providers → Add provider → Show more → Local ACP command</strong>"
    return ('<ol><li>'
            + bi('Select the local environment in T3 Code, then open ' + menu + '.',
                 '在 T3 Code 中选择本地环境，然后打开 ' + menu + '。',
                 inline=True)
            + '</li><li>'
            + bi('Name the provider <strong>CCFleet Gemini</strong>. Set the executable to '
                 '<code>ccfleet</code>. If T3 cannot find it, use the absolute path printed by '
                 '<code>command -v ccfleet</code>.',
                 '将服务商命名为 <strong>CCFleet Gemini</strong>，可执行文件填写 '
                 '<code>ccfleet</code>。如果 T3 找不到它，改填 <code>command -v ccfleet</code> '
                 '输出的绝对路径。', inline=True)
            + '</li><li>'
            + label("Enter one argument per row, in this order.", "按顺序填写参数，每行一个。",
                    inline=True)
            + '<ol><li><code>gemini</code></li><li><code>--acp</code></li></ol></li><li>'
            + bi('Save the provider. Open your local project and start a thread with '
                 '<strong>CCFleet Gemini</strong>. With the default project settings, choose '
                 '<strong>Supervised</strong> to approve each edit and test run on your Mac.',
                 '保存服务商，打开本地项目，用 <strong>CCFleet Gemini</strong> 新建对话。'
                 '使用默认项目设置时，选择 <strong>Supervised</strong> 可逐次确认'
                 '文件修改和本地测试。',
                 inline=True)
            + '</li></ol>')


def t3_gemini() -> str:
    return (
        '<section class="card" id="t3-gemini" aria-labelledby="t3-gemini-title">'
        '<h2 id="t3-gemini-title">'
        + label("Use Gemini in T3 Code on your Mac", "在 Mac 上通过 T3 Code 使用 Gemini",
                inline=True)
        + '</h2>'
        + bi('<p>T3 opens the project on your Mac. CCFleet connects T3 to Antigravity on your '
             'private Mac slot, where your Google account is signed in. This uses your '
             'Antigravity subscription. Requested file contents and test output go to the slot '
             'and may reach Google as model context.</p>',
             '<p>T3 打开的是这台 Mac 上的项目。CCFleet 将 T3 连接到专属 Mac 槽位上的 Antigravity，'
             '使用槽位上已登录的 Google 账号和 Antigravity 订阅。槽位收到的文件内容和测试输出'
             '也可能作为模型上下文发送给 Google。</p>')
        + '<h3>' + label("Pair each Mac once", "每台 Mac 只需配对一次", inline=True) + '</h3>'
        + bi('<p>Use your saved Gemini setup code. To create your first code, open '
             '<a href="/account/gemini#subscription">Gemini settings</a>, choose '
             '<strong>Create reusable setup code</strong> and save it in your password manager. '
             'The code is shown only once. On each trusted Mac, run these commands and paste it '
             'at the hidden prompt. This also applies to a Mac already paired with Claude.</p>',
             '<p>请使用已保存的 Gemini 配对码。首次创建时，打开 '
             '<a href="/account/gemini#subscription">'
             'Gemini 设置</a>，选择<strong>创建可重复使用的配对码</strong>，'
             '并将它保存在密码管理器中。配对码仅显示一次。'
             '在每台受信任的 Mac 上运行以下命令，按提示粘贴同一个码（输入不会显示）。'
             '已配对 Claude 的 Mac 也需要这一步。</p>')
        + '<details id="t3-install"><summary>'
        + label("Install CCFleet on a new Mac", "在新 Mac 上安装 CCFleet", inline=True)
        + '</summary>'
        + bi('<p>If the terminal cannot find <code>ccfleet</code>, install the client and add '
             'its directory to this terminal’s search path.</p>',
             '<p>如果终端找不到 <code>ccfleet</code>，请安装客户端，并将其目录加入当前终端的'
             '命令搜索路径。</p>')
        + '<pre><code>curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/'
          'laptop/install.sh | bash\nexport PATH="$HOME/.local/bin:$PATH"</code></pre></details>'
        + '<pre><code>ccfleet update\nccfleet pair gemini</code></pre>'
        + bi('<p>Pairing returns you to the terminal. Each Mac gets its own connection, '
             'which you can remove from the Gemini account page.</p>',
             '<p>配对完成后会返回终端。每台 Mac 都有独立的连接，可在 Gemini 页面单独移除。</p>')
        + '<h3>' + label("Set up T3 Code once", "首次配置 T3 Code", inline=True) + '</h3>'
        + t3_provider_steps()
        + '<h3 id="gemini-project-access">'
        + label("Let Gemini run project commands", "允许 Gemini 运行项目命令", inline=True)
        + '</h3>'
        + bi(
            '<p>With CCFleet 0.2.24, you can authorize development projects under your Mac '
            'home once. In a local terminal on the Mac that holds your files, run:</p>',
            '<p>从 CCFleet 0.2.24 起，可一次性授权 Mac 主目录下的开发项目。'
            '在存放文件的 Mac 上打开本地终端，运行以下命令。</p>',
        )
        + '<pre><code>ccfleet update\ncd ~\nccfleet gemini --full-access</code></pre>'
        + bi(
            '<p>Read the scope and type <code>ENABLE</code> once. The command saves the grant '
            'and exits. Use your existing Gemini pairing and provider in T3, the CCFleet '
            'terminal, Orca Quick Commands, or Herdr desktop and TUI. Open a project normally '
            'to use it immediately.</p>',
            '<p>核对授权范围后输入一次 <code>ENABLE</code>。命令保存授权后即退出。'
            '继续使用已有的 Gemini 配对和服务商，在 T3、CCFleet 终端、Orca Quick Commands '
            '或 Herdr 桌面版和 TUI 中打开项目即可。</p>',
        )
        + bi(
            '<p>You can also open <code>~</code> in the client and select a project from '
            'the conversation. For example, send <code>/workspace ~/code/app</code>. '
            'Send it again with a different project to switch in the same conversation. '
            'Use <code>/workspace</code> by itself to see which local directory Gemini is '
            'using. T3, Orca and Herdr keep their own project panels and terminal directories.</p>',
            '<p>也可以在客户端打开 <code>~</code>，然后在对话中发送 '
            '<code>/workspace ~/code/app</code> 选择项目。再次发送并换一个路径，即可在同一对话中'
            '切换项目。单独发送 <code>/workspace</code> 可查看 Gemini 当前使用的本地目录。'
            'T3、Orca 和 Herdr 自身的项目面板及终端目录保持原状。</p>',
        )
        + bi(
            '<p>The grant covers eligible projects you own, including Git projects and '
            'projects with a root manifest such as <code>package.json</code> or '
            '<code>pyproject.toml</code>. Home is a starting point for '
            'navigation. Shell commands run offline inside the selected project. Full access '
            'runs permitted actions without approval prompts. If an action is unavailable, '
            'CCFleet reports it. To remove the all-projects grant, run '
            '<code>ccfleet gemini --revoke-full-access</code> from <code>~</code>. '
            'Checkouts with a separate local grant keep that grant.</p>',
            '<p>授权范围包括你拥有的合格项目，例如 Git 项目或包含受支持开发配置文件的项目。'
            '主目录用于选择项目，Shell 命令在选中的项目内离线运行。启用完全访问后，'
            '已获授权的操作直接执行；无法执行的操作会返回错误，不弹出确认请求。'
            '在 <code>~</code> 运行 <code>ccfleet gemini --revoke-full-access</code> '
            '可撤销跨项目授权。之前单独授权的项目仍保留各自的权限。</p>',
        )
        + bi(
            '<p>To grant a single folder, use the existing CCFleet 0.2.23 flow. Run the '
            'following commands from that project directory.</p>',
            '<p>如仅需授权一个目录，可沿用 CCFleet 0.2.23 的单项目方式。'
            '在该项目目录运行以下命令。</p>',
        )
        + '<pre><code>ccfleet update\nccfleet gemini --full-access</code></pre>'
        + bi(
            '<p>Check the displayed folder and publishing destination. Type <code>ENABLE</code> '
            'once to save the grant. When the command returns, run <code>ccfleet gemini</code>, '
            'or open the same project in T3, Orca or Herdr. Use your existing Gemini provider '
            'or terminal command. Pairing is separate.</p>',
            '<p>核对显示的目录和发布目标，输入一次 <code>ENABLE</code> 保存授权。命令结束后，'
            '运行 <code>ccfleet gemini</code>，或在 T3、Orca、Herdr 中打开同一目录，沿用已配置的 '
            'Gemini 服务商或终端命令。设备配对是单独的步骤。</p>',
        )
        + bi(
            '<p>In a trusted project, Gemini can edit files, run tests, build and make local '
            'Git commits without further approval prompts. Commands run offline inside the '
            'checkout. Command output can be sent to Gemini. Install any needed dependencies '
            'yourself first. For Git writes, use '
            'a standalone clone with its own <code>.git</code> directory. Linked worktrees '
            'usually keep their Git data outside the checkout. Without a grant, projects use '
            'the supervised flow. Project commands require a compatible slot.</p>',
            '<p>项目授权生效后，Gemini 可以修改文件、运行测试、构建项目和创建本地 Git 提交，'
            '无需逐次确认。命令在项目目录内离线运行，输出可能会发送给 Gemini。'
            '请先自行安装所需依赖。需要写入 Git 时，'
            '请使用带有独立 <code>.git</code> 目录的克隆。链接工作树通常将 Git 数据保存在目录'
            '之外。未授权的项目使用逐次确认模式。项目命令需要兼容的槽位。</p>',
        )
        + bi(
            '<p>All-projects access covers local development. To publish, use a destination '
            'pinned to the checkout and sign in locally with <code>gh</code> for GitHub.com or '
            '<code>glab</code> for GitLab.com. At authorization, CCFleet uses a recognized '
            '<code>origin</code> by default. If it cannot identify a repository, local commands '
            'still work. To add publishing to an all-projects grant, run the following in the '
            'specific checkout and supply its destination URL.</p>',
            '<p>如需发布，请在本机用 <code>gh</code> 登录 GitHub.com，或用 <code>glab</code> '
            '登录 GitLab.com。授权时 CCFleet 默认使用可识别的 <code>origin</code>。未识别出仓库时，'
            '仍可运行本地命令。已启用跨项目授权时，请在需要发布的项目目录执行以下命令，'
            '指定该项目的仓库网址。</p>',
        )
        + '<pre><code>ccfleet gemini --full-access --publish-to https://github.com/OWNER/REPO</code></pre>'
        + bi(
            '<p>Ask Gemini to push the current branch, then open a pull request or merge '
            'request in that repository. The publisher verifies that the remote branch '
            'already points to the exact local commit before opening a review. Google sign-in '
            'stays on the slot. Revoke access from the project directory at any time, '
            'including offline.</p>',
            '<p>可以让 Gemini 先推送当前分支，再在该仓库创建拉取请求或合并请求。创建请求前，'
            '发布工具会验证远端分支是否已指向当前本地提交。Google 登录保留在槽位上。'
            '随时可在项目目录运行以下命令撤销授权，离线也可执行。</p>',
        )
        + '<pre><code>ccfleet gemini --revoke-full-access</code></pre>'
        + '<h3 id="gemini-recovery">'
        + label("Recover a Gemini conversation", "恢复 Gemini 对话", inline=True) + '</h3>'
        + bi(
            '<p>CCFleet 0.2.22 can resume a conversation after a connection drops. If you use '
            '0.2.21, run <code>ccfleet update</code> on the Mac that holds your project before '
            'opening your next Gemini session. Your private Mac slot also needs the updated '
            'Gemini service. Your existing pairing still works.</p>',
            '<p>从 CCFleet 0.2.22 起，连接中断后可以恢复原对话。如果你使用的是 0.2.21，请在'
            '存放项目的 Mac 上运行 <code>ccfleet update</code>，再开始下一次 Gemini 对话。'
            '专属 Mac 槽位也需要新版 Gemini 服务。原有配对仍然有效。</p>',
        )
        + bi(
            '<p>In T3, keep the same thread open on the paired Mac. To change your instruction '
            'while Gemini is working, use <strong>Steer</strong>. If the connection drops, '
            'review your local changes first. CCFleet leaves completed edits in place, expires '
            'pending approvals and never resends the interrupted request. Ignore any old '
            'approval card that T3 still displays.</p>',
            '<p>在已配对的 Mac 上保持当前 T3 对话打开。要在 Gemini 工作时修改指令，可使用 '
            '<strong>Steer</strong>。如果连接中断，先检查本地文件的变化。已完成的修改会保留，'
            '待确认的操作会失效，CCFleet 不会重发中断的请求。如果 T3 仍显示旧的确认卡片，'
            '可以忽略。</p>',
        )
        + bi(
            '<p>Within five minutes of the disconnect, send your next instruction in that thread '
            'to try to resume, or enter <code>/reconnect</code> to check the connection without '
            'sending a model prompt. If CCFleet says the old conversation is unavailable, enter '
            '<code>/reconnect</code> when prompted to start a fresh conversation in the same '
            'thread. Give Gemini the context it needs before continuing.</p>',
            '<p>断线后五分钟内，在原对话中发送下一条指令即可尝试恢复。也可以输入 '
            '<code>/reconnect</code> 检查连接，这不会向模型发送消息。如果 CCFleet 提示原对话'
            '已无法恢复，可按提示输入 <code>/reconnect</code>，在当前 T3 对话窗口重新开始。'
            '继续之前，请向 Gemini 说明所需的背景。</p>',
        )
        + '</section>'
    )


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
        + bi('<p><strong>Gemini on CCFleet.</strong> On a paired Mac, '
             '<a href="#t3-gemini">open your local project in T3 Code</a> or run '
             '<code>ccfleet gemini</code> in the project directory for a terminal chat. '
             'Both use the Antigravity subscription on your private Mac slot.</p>',
             '<p><strong>通过 CCFleet 使用 Gemini</strong>在已配对的 Mac 上，'
             '<a href="#t3-gemini">用 T3 Code 打开本地项目</a>，或在项目目录运行 '
             '<code>ccfleet gemini</code> 开始终端对话。两种方式都使用专属 Mac 槽位上的 '
             'Antigravity 订阅。</p>')
        + bi('<p>The table also lists sign-in options that tool publishers document for their '
             'own agents. Those entries identify where the agent runs and which plan it uses. '
             'Publisher documentation checked November 21, 2026.</p>',
             '<p>下表也列出各工具官方文档为自家代理提供的登录方式，说明代理在哪里运行、'
             '使用哪个套餐。各工具官方文档核对日期为 2026 年 11 月 21 日。</p>')
        + '<div class="doc-table-scroll" role="region" tabindex="0" '
        'aria-labelledby="agent-compatibility-title">'
        '<table><thead><tr>' + ''.join('<th scope="col">' + label(*titles, inline=True) + '</th>'
                                       for titles in columns)
        + '</tr></thead><tbody>' + ''.join(_row(*row) for row in ROWS) + '</tbody></table></div>'
        + bi('<p>For direct Google sign-in on your computer, use the official Antigravity agent. '
             'Google retired personal sign-in in the older Gemini CLI in June 2026. A community '
             'plug-in or a matching model name alone does not establish subscription access. '
             '<a href="https://antigravity.google/docs/faq">Google FAQ</a> · '
             '<a href="https://developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals">'
             'Gemini CLI deprecation notice</a> · <a href="https://code.claude.com/docs/en/legal-and-compliance">'
             'Anthropic authentication policy</a></p>',
             '<p>如需在本机直接登录 Google，请使用官方 Antigravity 代理。Google 已于 2026 年 6 月'
             '停止旧版 Gemini CLI 的个人登录。仅凭社区插件或同名模型无法确认是否使用订阅额度。'
             '<a href="https://antigravity.google/docs/faq">Google 常见问题</a> · '
             '<a href="https://developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals">'
             'Gemini CLI 停用公告</a> · <a href="https://code.claude.com/docs/en/legal-and-compliance">'
             'Anthropic 认证政策</a></p>') + '</section>'
    )
