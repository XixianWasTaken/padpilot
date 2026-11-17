<p align="center"><img src="docs/logo.svg" alt="padpilot" width="760"></p>

<p align="center"><a href="README.md">English</a> · <b>中文</b></p>

# padpilot：用 PS5 手柄操控 Claude Code / Codex

这个插件让 DualSense 手柄的**呼吸灯、震动和自适应扳机**实时显示 agent 在做什么，再用**按键**来审批、中断、选菜单、发送预设提示词。它同时支持 Claude Code 和 OpenAI Codex CLI，手柄可以走 USB 或蓝牙。

```
 Claude Code / Codex ──hooks──▶ padpilot daemon ──HID──▶ DualSense（灯 / 震动 / 扳机）
        ▲                          │   ▲
        └──── PTY 键盘注入 ◀────────┘   └──── 按键 / 摇杆 / 触摸板
            (padpilot run claude)
```

它由三部分组成：

- **daemon**（`padpilot daemon`）：独占手柄，把事件渲染成灯效和震动，并把按键转发给当前焦点会话。第一次用到时会自动启动，空闲 4 小时后自动退出。
- **hooks**：Claude Code 插件自带。Codex 通过 `padpilot install-codex` 安装。每个 hook 约 40ms，任何错误都不会影响 agent 运行。
- **PTY 包装器**（`padpilot run claude` / `padpilot run codex`）：终端照常显示，手柄按键会被转换成键盘输入。它会自动识别光标键模式（DECCKM）和 kitty 键盘协议。

## 安装

需要 macOS 或 Linux，以及 Python ≥ 3.8。

```bash
git clone https://github.com/XixianWasTaken/padpilot.git
cd padpilot

# 1. 运行时环境（在 ~/.padpilot/venv 里安装 hidapi）
./bin/padpilot setup
ln -sf "$PWD/bin/padpilot" ~/.local/bin/padpilot   # 让 padpilot 进 PATH

# 2. Claude Code 插件
claude plugin marketplace add "$PWD"
claude plugin install padpilot@padpilot

# 3. （可选）Codex hooks，写入 ~/.codex/hooks.json，会先备份原文件
padpilot install-codex      # 然后在 Codex 里执行一次 /hooks 信任这些 hook

# 4. 检查
padpilot doctor
padpilot demo               # 彩虹灯 + 震动
padpilot watch              # 按手柄，终端会打印按键名
```

日常使用（建议加别名）：

```bash
alias cc='padpilot run claude'
alias cx='padpilot run codex'
```

不经过 `padpilot run` 也能用（比如 Claude 桌面端、IDE）：灯效和震动照常工作，**权限审批改由 hook 处理**，具体见下方的「审批」一节。在 macOS 上，按键还能直接操作 **Claude 桌面 app**，见「桌面 app」一节。

## 灯效 / 震动 / 扳机

| 状态 | 灯 | 震动 | 触发事件 |
|---|---|---|---|
| idle 空闲 | 灰白慢呼吸 | 会话开始时轻点两下 | SessionStart |
| thinking 思考 | 蓝色呼吸 | – | UserPromptSubmit / PostToolUse |
| tool 调工具 | 按工具类型变色的快呼吸，每次调用闪一下（见下表） | 按工具类型 | PreToolUse |
| **permission 等审批** | **橙色闪烁** | **强双震，20 秒后再提醒，最多 3 次** | PermissionRequest / Notification(permission_prompt) |
| waiting 等你输入 | 黄色呼吸 | 轻推一下 | Notification(idle_prompt…) / Elicitation |
| done 完成 | **绿色常亮，按任意键才熄**（最多保留 12 小时）；10 分钟没理会再提醒一次；**本轮超过 60 秒则先放 2 秒彩虹** | 成功节奏；长任务是更长的 fanfare；提醒时轻推一下 | Stop |
| error 出错 | 红色闪烁 | 长震 | StopFailure（API 错误） |
| 工具失败 | 红闪 | buzz | PostToolUseFailure |
| compacting 压缩 | 紫色呼吸 | – | PreCompact |

| listening 有 app 在用麦克风 | 白色快呼吸 + 麦克风灯亮 | 开麦轻点一下，关麦再点一下 | 任何 app 开麦（比如 Claude 的听写） |
| 低电量（≤15%，没在充电） | 红闪 | 三下长震，每 5 分钟最多一次 | 手柄电量 |
| **按了但做不了**（这个程序没有该功能、没有可重试的消息、卡片上没有这个按钮……） | 红闪 | **两下短震（“不行”）** | 按键 |
| Claude 给出带回复按钮的下一步卡片 | – | 轻点两下 | PreToolUse(wrap_up) |

工具类型（`tool_kinds`，按工具名正则匹配，Claude 和 Codex 的工具名都覆盖了）：

| 工具 | 颜色 | 震动 |
|---|---|---|
| Bash / shell | 橙 | 稍重的 tick |
| Edit / Write / apply_patch | 品红 | 两下低频 thud |
| Read / Grep / Glob | 青 | 轻 tick |
| WebFetch / WebSearch | 蓝 | 轻 tick |
| Task / Agent（子 agent） | 紫 | nudge |
| MCP 工具（`mcp__*`） | 青绿 | 轻 tick |
| 其他 | 白闪，状态保持默认青色 | 轻 tick |

自适应扳机：
- **permission**：**R2 有段落感阻力，扣到底会“咔”一下 = 批准**；L2 有轻阻力。
- **thinking / tool / compacting**（agent 在干活）：**L2 有一点阻力**，提示“扣到底 = Esc 中断”。
- 其他状态扳机放松。

几个指示灯：
- **玩家灯**亮 1 / 2 格（中间对称）且 agent 不在干活时，表示**下一步卡片上有 1 / 2 个回复按钮可以用 R1 组合键选**。
- 其他时候**玩家灯**是本轮的进度条：工具调用次数达到 1 / 3 / 6 / 11 / 21 时依次多亮一格，开始新一轮时清空。同时开着两个以上 `padpilot run` 会话时，玩家灯改为显示焦点是第几个会话。
- **麦克风灯亮**表示有 app 正在录音，或手柄输入已锁定。
- **按 PS 键**，玩家灯和光条会显示电量（红到绿）。
- **按住 △ 拨右摇杆**时，光条变成所选方向的颜色（8 个方向 8 种色相）。
- **上下文用量**：每轮结束时从会话记录读出最后一次请求用了多少 token。超过 80% 时，完成的绿灯会在绿和琥珀之间慢慢渐变，提醒你快该压缩或开新会话了。按 PS 时玩家灯显示用了几成（每格 20%），光条颜色还是电量。窗口大小按模型估：`claude-opus-5*` 按 1M，其他按 200k；实际用量超过估计值时自动按 1M 算。不准的话在配置 `"context_windows"` 里改。

## 按键

**桌面 app 和 CLI 用同一套布局**：同一个键在 Claude 桌面 app、Claude Code CLI（`padpilot run claude`）、Codex CLI（`padpilot run codex`）里做同一件事。映射写的是“功能”（提交、取消、清空输入……），按下时再换算成目标程序实际使用的组合键。某个程序没有这个功能时，按了不发任何按键，光条红闪一下。

| 按键 | 功能 | 桌面 app | Claude CLI | Codex CLI |
|---|---|---|---|---|
| ✕ / R2 扣到底 | 提交 / 确认 / 批准 | Enter | Enter | Enter |
| ○ / L2 扣到底 | 取消 / 中断 / 拒绝 | Esc | Esc | Esc |
| □ | **清空输入框** | ⌘A + 删除 | Ctrl+U + 退格（逐行） | - |
| △ 短按 | 输入 `continue` 再提交 | 同左 | 同左 | 同左 |
| **△ 按住 + 右摇杆** | **快捷语轮盘**：8 个方向 8 句预设，松开 △ 时清空输入框并填进去（不发送，可以再改） | ✓ | ✓ | ✓ |
| **R1 + ✕ / ○ / □ / △** | **选下一步卡片上的第 1 / 2 / 3 / 4 个按钮**（等于点击它） | ✓ | - | - |
| R1 短按 | 输入 `/`（斜杠命令菜单） | ✓ | ✓ | ✓ |
| **R3（按下右摇杆）** | **重试**：把你上一条消息重新发一遍 | ✓ | ✓ | ✓ |
| L1 短按 | 切换权限模式 | - | Shift+Tab | Shift+Tab |
| **L1 + ✕** | 排队发送（不打断正在跑的回合） | ⌘↩ | Ctrl+X Enter | - |
| **L1 + ○** | 停掉所有子 agent | - | Ctrl+X Ctrl+K | - |
| **L1 + □** | 撤销输入 | ⌘Z | Ctrl+_ | - |
| **L1 + △** | 外部编辑器 | - | Ctrl+G | - |
| **L1 + ↑** | 选模型 | ⌘⇧I | Meta+P | - |
| **L1 + ↓** | 思考 / effort | ⌘⇧E（effort 选择器） | Meta+T（思考开关） | - |
| 十字键 / 左摇杆 | 方向键，按住连发 | ✓ | ✓ | ✓ |
| 右摇杆 ↑↓ / 触摸板上下滑 | PageUp / PageDown | ✓ | ✓ | ✓ |
| **右摇杆 ← / →** | 上一个 / 下一个会话（不连发） | ⌘⇧[ / ⌘⇧] | 切换 `padpilot run` 会话 | 同左 |
| Options | 查看改动 / 过程 | ⌘⇧D（Show Changes） | Ctrl+O（transcript） | Ctrl+T |
| Create 短按 | 切换焦点会话（多个 `padpilot run` 时） | ✓ | ✓ | ✓ |
| **Create 长按** | **新会话** | ⌘N | 清空输入 + `/clear` | `/new` |
| 触摸板按下 | 回退 / 编辑上一条消息 | Esc Esc | Esc Esc | Esc Esc |
| L3 | 命令面板 | ⌘K | - | - |
| 触摸板左滑 | 搜索历史 | - | Ctrl+R | Ctrl+R |
| 麦克风键 | **语音输入（听写）**：短按 = 开 / 关；**按住说话，松开结束**（按住超过 0.5 秒） | ⌘D（Claude 自带听写） | - | - |
| PS 短按 | 显示电量 | ✓ | ✓ | ✓ |
| PS 长按 | 强制中断（按住到“嗒”一下再松开） | Esc | Ctrl+C（连按两次退出） | Ctrl+C |
| **PS + ↑ / ↓** | 光条亮度 | ✓ | ✓ | ✓ |
| **PS + → / ←** | 震动强度（调完震一下给你感受） | ✓ | ✓ | ✓ |
| **PS + □** | 空闲时光条开 / 关 | ✓ | ✓ | ✓ |
| **PS + △** | 手柄喇叭提示音开 / 关 | ✓ | ✓ | ✓ |
| **PS + 麦克风** | 锁定 / 解锁手柄输入（麦克风灯亮 = 锁定） | ✓ | ✓ | ✓ |
| 触摸板右滑 | 空闲，可自己配置 | | | |

桌面 app 的快捷键来自 Claude app 自带的快捷键帮助表（⌘⇧[ / ⌘⇧] 是它列出的 Previous / Next session，⌘↩ 是 Queue for later，⌘⇧I 是 Open model menu，⌘⇧E 是 Open effort selector，Esc Esc 是 Rewind）。

组合键的规则：**按住 PS、R1 或 L1，再按另一个键**。PS、R1、L1 自己的短按 / 长按在松开时才触发，用过组合键就不触发，所以不会误按出 `/` 或电量。长按到时间会“嗒”轻震一下，这时松开就是长按。PS 层的设置会写进 `~/.padpilot/config.json`，重启后保留。

#### 下一步卡片（R1 + 面键）

Claude 桌面 app 在回合结束时可能给出带按钮的卡片（比如“先做第一批 / 全部做”）。插件的 hook 在 Claude 调用 `wrap_up` 时记下按钮文字，**R1 + ✕ 选第 1 个，R1 + ○ 选第 2 个**，效果和点击一样：清空输入框，输入按钮文字，发送。卡片出现时手柄轻点两下，玩家灯亮出按钮个数。“提交 / 建 PR”这类 app 自己处理的按钮不能用手柄按，按了是“不行”的两下短震；发了新消息后旧卡片作废。Claude 中途弹出的提问框（几个选项加 Skip / Submit）也用 **R1 + 面键** 选第 1–4 个选项：单选题选完直接提交；多选题每按一次勾上或取消，选好按 ✕ 提交。提问框开着时光条是等你回答的黄色；Claude Code 为提问框发的权限请求不会被当成审批，所以 ✕ 不会被它吃掉。daemon 从 hook 拿到选项文字，再用辅助功能接口找到这些选项，聚焦后按一下空格来选，然后按下 Submit。“Other” 自己写答案仍要用鼠标。提交一页大约要一秒，这期间再按的那一下会被丢掉，不会落到下一页上。卡片和回合结束后的绿灯会保留 12 小时。daemon 重启后（比如闲置 4 小时自动退出过）它不记得之前的卡片，要等下一张卡片出现才能选。

#### 重试（R3）

不小心按了中断（○ / L2 / PS 长按），或者想再跑一遍：按 **R3**。hook 记下你最近一次发出的消息（只存在 daemon 内存里；daemon 重启过的话，要等你再发一条消息，R3 才有东西可重发），R3 会清空输入框、原样再输一遍、发送。**桌面 app 里只填不发**：桌面 app 不告诉我们屏幕上是哪个会话，最近那条消息可能来自另一个会话，所以填好后由你看一眼再按 ✕。多行消息在桌面 app 里用 Shift+Enter 换行，不会提前发出去。agent 还在干活时按 R3 会“不行”两下短震，防止重复提交；如果你确定它已经停了（比如是用鼠标点停止的，daemon 收不到事件），**2 秒内再按一次 R3** 就强制重试。但那个会话**几秒内（`retry_live_s`，默认 8 秒）还有 hook 事件**时，说明它确实还在跑，连按也不发：桌面 app 认不出你眼前是哪个会话，强制重发可能把另一个会话的消息输进当前窗口。

#### 快捷语轮盘（△ 按住）

默认 8 句（上开始顺时针）：继续 / 运行测试并修复失败的用例 / 解释一下你刚才做了什么 / 先停下来，问我再继续 / 提交代码 / 给这部分写测试 / 总结一下改动 / 继续修这个 bug。在配置里改 `"prompt_wheel": [...]`。拨到新方向时轻震一下，摇杆回中不会取消，松开 △ 才确定。

`padpilot keys` 会按你当前的设置打印这张表。

### 自动适配你的快捷键

不用把快捷键改回默认。每次按键时 daemon 都会检查下面两个文件，改动后立即生效，不用重启：

- **Claude Code CLI**：`~/.claude/keybindings.json`（设置了 `CLAUDE_CONFIG_DIR` 时用那个目录）。按文档规则叠加在默认快捷键上，包括 `null` 取消绑定和 `ctrl+x ctrl+l` 这类连续按键。比如你把 `chat:cancel` 改成 `ctrl+q`，○ 就会发 `ctrl+q`。等审批时 ✕ / ○ 用 `Confirmation` 那组绑定。终端发不出来的组合键（比如带 `cmd` 的）会被跳过，改用同一功能的其他绑定；一个都发不出来时，红闪提示。文件格式写错时，退回默认快捷键，`padpilot keys` 会显示原因。
- **Claude 桌面 app**：读取 `~/Library/Application Support/Claude/claude_desktop_config.json` 里的听写快捷键（`preferences.dictationShortcut`）。默认 ⌘D；改过的话用你的组合键；关掉了的话，按麦克风键红闪。
- 读不到的：桌面 app 里“回车发送还是 ⌘回车发送”看起来是账号级的网页设置，本地没有存，所以 ✕ 固定发回车。Codex CLI 没有快捷键配置文件，用它的固定快捷键。

有会话请求审批时，焦点会自动切到那个会话，所以按 ✕ 批准的一定是正在等审批的那个。请求刚出现的 0.6 秒内按下的 ✕ 不算数（那一下本来是按给别处的），手柄短震两下，再按一次才生效。

## 手柄喇叭提示音

用 USB 连着时，macOS 把手柄当成一块 4 声道声卡。状态变化时，手柄喇叭放一个系统音效：完成 Glass，要审批 Ping，出错 Basso。按住 **PS 再按 △** 开 / 关（会保存，打开时放 Pop）。蓝牙连接时没有声音。

几个实测过的细节（2026-11-16，USB）：

- 喇叭放的是声卡的**右声道**（第 2 声道），所以音效只放在右声道上。`say` 这类单声道输出放不到喇叭上。
- 音频流**关闭时手柄会发出一声像“F”的噪音**。所以只在放提示音的 `hold_s` 秒内把声音切到喇叭，音频流再多开 `tail_ms` 毫秒的静音，等喇叭关了才关流，就听不到了。
- 播放器只往手柄这块声卡放；找不到它（蓝牙、拔掉了）就什么都不放，不会改从 Mac 的扬声器出声。它在单独的进程里跑，出问题也不影响 daemon。

```json
"sounds": {
  "enabled": true,
  "volume": 100,
  "cues": {"done": "Glass", "permission": "Ping", "error": "Basso", "sounds_on": "Pop"}
}
```

`cues` 的键是会话状态（`done` `permission` `error` `waiting` …），值是 `/System/Library/Sounds` 里的音效名（Basso Blow Bottle Frog Funk Glass Hero Morse Ping Pop Purr Sosumi Submarine Tink），或者一个声音文件的路径；删掉某个键就不放。想先听一下：`python3 -m padpilot.sound play Hero`。`volume` 是喇叭音量（0–100），比 100 小的值还没在真手柄上试过。

## 审批（不走 `padpilot run` 时）

Claude 桌面端、IDE 或直接运行的 `claude` / `codex` 没有 PTY 可以注入按键。这时 `PermissionRequest` hook 会等手柄输入，最长 45 秒：

- ✕ 或 R2：**批准**
- ○ 或 L2：**拒绝**，拒绝消息会返回给 agent
- △：交回屏幕上的对话框处理
- 超时，或手柄没连接：直接显示正常的对话框
- 请求出现后 0.6 秒内（`approval_grace_ms`）按的 ✕ / ○ 不算数，手柄短震两下，再按一次
- 练习模式下不等手柄，直接显示对话框
- `padpilot press` 和 `padpilot send` 不能回答权限请求，只有真手柄可以

注意：在这 45 秒里屏幕上不会出现审批对话框。不想要这种行为的话，在配置里把 `"hook_approval_timeout_s"` 设为 `0`。设为 `0` 后，桌面 app 会直接显示审批卡片，但那张卡片不认手柄发的 Enter 和 ⌘↩（2026-11-16 实测），只能用鼠标点。

## 桌面 app（macOS）

没有 `padpilot run` 会话时，daemon 会把按键模拟成真实的键盘按下，发给**最前面的 Claude 桌面 app**（`com.anthropic.claudefordesktop`）。别的 app 在前面时不发，手柄光条红闪一下表示没送达。有 `padpilot run` 会话时按键都发给终端，只有一个例外：Claude 桌面 app 在最前面、里面开着提问框、而终端那边没有在提问或等待时，R1 + 面键和提交键（✕、R2）会去回答桌面 app 的提问框。提问框不在屏幕上时（它属于另一个会话），按键照常处理：✕ 就是回车。

第一次使用要给权限：

```bash
padpilot desktop    # 弹出系统对话框；或到 系统设置 → 隐私与安全性 → 辅助功能 打开 “Python”
padpilot stop && padpilot desktop   # 授权后再跑一次，显示 ok 就好了
```

- 授权给的是 daemon 用的 Python（`~/.padpilot/venv` 背后的 Python.app）。daemon 以独立进程启动，所以不管是 Claude hook、终端还是 `padpilot start` 拉起它，用的都是这一个授权。
- 因此 daemon 不从项目目录加载代码，而是在启动时复制一份到 `~/.padpilot/runtime/`（macOS 不允许这样启动的进程读取桌面、文稿等文件夹）。
- 按键布局见上面的「按键」一节。
- **麦克风键 = Claude app 自带的听写**（输入框右边那个麦克风）：按一下开始说话，再按一下停止；或者按住说话、松开结束。快捷键自动从 Claude 设置里读取。听写期间光条白色快呼吸、麦克风灯亮。这是通过检测“有没有 app 在录音”实现的，所以用别的方式开的听写也会亮。
- 有 `padpilot run` 会话时按键优先发给它。
- 想让别的 app 也接收按键，把 bundle id 加进配置的 `"desktop": {"apps": [...]}`；想关掉就设 `"enabled": false`。

## 配置

`~/.padpilot/config.json` 会深度合并到 [padpilot/config.py](padpilot/config.py) 的 `DEFAULTS` 上，只写想改的部分即可：

```json
{
  "brightness": 0.6,
  "rumble_scale": 0.5,
  "hook_approval_timeout_s": 0,
  "mapping": {
    "triangle": ["text:继续，并在完成后运行测试", "act:submit"],
    "l3": "act:external_editor"
  },
  "states": { "thinking": { "light": { "kind": "breathe", "color": [255, 0, 180], "period": 3 } } }
}
```

映射值支持以下几种：
- 功能：`act:submit` `act:cancel` `act:clear_input` `act:cycle_mode` `act:transcript` `act:todos` `act:history_search` `act:external_editor` `act:interrupt` `act:rewind` `act:dictation` `act:review` `act:new_session` `act:menu` `act:prev_session` `act:next_session` `act:queue_submit` `act:kill_agents` `act:undo` `act:model_picker` `act:thinking`。末尾加 `?`（如 `act:clear_input?`）表示可选：目标程序没有这个功能时跳过这一步，而不是整个取消。按目标程序和你的快捷键设置换算成实际按键，**推荐用这种**。
- 按键名：`enter` `esc` `tab` `shift+tab` `up` `pageup` `ctrl+<字母>` `alt+<字母>`，桌面 app 还支持 `cmd+<字母>`
- `text:…`：输入一段文字
- 内置动作：`@cycle_focus` `@battery` `@toggle_lock` `@retry` `@choose:1`…`@choose:4` `@brightness_up/down` `@rumble_up/down` `@idle_light` `@sounds` `@prev_session` `@next_session` `@none`
- 映射的键名可以是组合（`"ps+up"`、`"r1+cross"`，修饰键列表在 `"modifiers"`）、长按（`"create_hold"`）、松开（推送说话的 `"mic_up"`）
- 以上几种组成的列表：按顺序执行，每步间隔 `sequence_gap_ms`

`profiles.claude`、`profiles.codex`、`profiles.desktop` 可以只给某一个程序覆盖映射，默认都是空的，保证布局处处一致。改完配置后执行 `padpilot stop`，下次用到时会自动用新配置重启。

## 其它命令

```bash
padpilot status            # JSON：手柄、电量、会话状态
padpilot send "写个测试"    # 往焦点会话（或桌面 app）输入文字并回车；--no-enter 只输入不回车
padpilot desktop           # macOS：桌面 app 控制的权限和状态
padpilot keys              # 按键布局，以及按你当前的快捷键设置，每个键在各个程序里实际发什么
padpilot press cross       # 模拟按键，方便调试和写脚本（不能用来回答权限请求）
padpilot tutorial          # 打开新手教程网页；网页开着就是练习模式
padpilot manual            # 打开中英双语的说明书
padpilot watch --practice  # 终端版练习模式：灯和震动照常，但不往任何 app 发键；实时显示每个键按了多久、识别成什么（守护进程重启后自动重连）
padpilot rumble success    # 播放一段震动
padpilot uninstall-codex
```

插件里的斜杠命令：`/padpilot:tutorial`、`/padpilot:status`、`/padpilot:demo`、`/padpilot:doctor`、`/padpilot:desktop`。每个命令只预先放行它自己那个 `padpilot` 子命令。

### 新手教程

在 Claude Code 里输入 `/padpilot:tutorial`（或在终端运行 `padpilot tutorial`），会在浏览器里打开中英双语的新手教程：一步一个键，用真手柄、点图或用键盘都能练。

- 页面由守护进程在 `http://127.0.0.1:8737/` 提供（端口可在配置里用 `"tutorial_port"` 改，设成 0 就关掉）。只有这个页面自己能用它：Host 必须是本机，Origin 必须是这个地址。
- **页面开着就是练习模式**：灯和震动照常，按键只在页面里生效，不会发给 Claude。点页面底下的「退出练习模式」或关掉页面就退出练习，不用开终端；退出后点「重新进入练习」可以接着练。守护进程重启后，页面会自己重连。
- 页面通过守护进程读按键，所以手柄一直**独占**（macOS 上 hidapi 的默认方式）：macOS 看不到 PS 键，不会打开 Apple「游戏」App。不独占时按 PS 会打开它（2026-11-16 实测）。麦克风键也能读到，浏览器自带的手柄接口读不到这个键。
- 教程里的光条颜色会同步到真手柄的光条上。
- 教程源文件是 [padpilot/tutorial.html](padpilot/tutorial.html)，也发布成了 claude.ai 上的页面；那个版本读不到手柄，只能点图或用键盘。
- 想让别的程序（比如浏览器的手柄接口）也能读手柄，在配置里设 `"share_controller": true`。代价是按 PS 会打开「游戏」App。

## padpilot 会运行、读取和发送什么

所有东西都在你自己的电脑上运行。padpilot 没有服务器，不收集使用数据，会话里的任何内容都不会离开这台电脑。完整列表：

- **Hooks**：Claude Code 在这些事件上运行 `bin/padpilot hook claude`：SessionStart、SessionEnd、UserPromptSubmit、PreToolUse、PermissionRequest、PermissionDenied、PostToolUse、PostToolUseFailure、Notification、Stop、StopFailure、SubagentStop、PreCompact、PostCompact、Elicitation。每次运行通过本机的 Unix socket（`~/.padpilot/daemon.sock`）给守护进程发一条小消息，内容是事件名、会话 id、工作目录和工具名。UserPromptSubmit 时还带上你发的消息，只存在守护进程的内存里，供 R3 重发用，不写到磁盘。Claude 给出下一步卡片时，带上卡片上按钮的文字；弹出提问框时，带上选项文字和是否多选（不带问题本身）。
- **权限审批**：不走 `padpilot run` 时，PermissionRequest hook 最多等 45 秒（`hook_approval_timeout_s`）。按 ✕ 回答允许，按 ○ 回答拒绝；不按的话，Claude Code 照常弹出它自己的对话框。提问框的权限请求不会被拦下等待，直接在提问框里回答。请求出现后 0.6 秒内（`approval_grace_ms`）的按键不算回答；`padpilot run` 会话里刚弹出的确认框，这段时间内也不接受回车。练习模式下不拦请求。`padpilot press` 和 `padpilot send` 不能回答权限请求。
- **守护进程**：第一个 hook 会启动一个后台进程，通过 USB 或蓝牙（HID）打开手柄。4 小时没有活动会自动退出（`idle_shutdown_minutes`），也可以用 `padpilot stop` 停掉。插件的代码比 daemon 正在运行的新时（比如更新之后），下一次会话开始、发送消息或权限请求会自动换成新的 daemon；新代码加载不了的话就不换。换过之后，它不记得各会话之前的卡片和上一条消息。
- **唯一一次下载**：第一次需要守护进程时，padpilot 会创建 `~/.padpilot/venv`，并用 pip 从 PyPI 安装 `hidapi==0.15.0`。这是 padpilot 唯一会下载的东西。
- **读取的文件**：
  - 当前 Claude Code 会话记录的末尾（hook 事件里给出的那个文件），只为了一件事：最后一次请求用了多少 token，用来做上下文提醒。不用也不保存任何消息内容。你最后一条消息和下一步卡片都来自 hook 事件，不读会话记录。
  - `~/.claude/keybindings.json`，以及 Claude 桌面 app 的 `claude_desktop_config.json` 里的听写快捷键，这样按键发的是你实际用的快捷键。
  - `~/.padpilot/config.json`，你的设置。
- **写入的文件**，都在 `~/.padpilot/` 里：
  - `config.json`，只在你用 PS 组合键改设置时写；
  - `daemon.log`，记录按键名、会话状态和发出的按键（输入的文字只记字数，不记内容）；
  - `daemon.stderr`、socket 和 pid 文件；
  - `sounds/`，用 `afconvert` 转换给手柄用的 macOS 系统提示音；
  - `venv/`。

  `padpilot install-codex` 只在你运行它时才写 `~/.codex/hooks.json`，写之前先备份。
- **按键（macOS）**：有辅助功能权限时，守护进程用 Quartz CGEvent 给 Claude 桌面 app 发按键。每次发之前都用 `lsappinfo` 确认 Claude app 在最前面，别的 app 在前面时什么都不发。打字、发送、清空、中断（Esc）之前，如果焦点在按钮或侧边栏上，它会用辅助功能接口把焦点移回输入框（焦点在文字框、菜单或对话框里时不动）。为此它会打开 Claude app 的辅助功能树，只读元素的类型和标签，不读里面的文字。快捷键里的字母按你当前键盘布局上的位置发（法语键盘上的 Cmd+A 不会变成 Cmd+Q）；布局由一个很快结束的子进程读取，最多每 3 秒读一次，而且只在发这类键时读。`padpilot run` 则是直接输入到它自己的终端会话里。
- **麦克风**：通过 CoreAudio 查询有没有 app 正在录音，用来点亮手柄的麦克风灯。它从不打开麦克风，也从不录音。
- **声音**：提示音是 macOS 系统声音，在手柄自带的喇叭上播放（只在 USB 连接时）。
- **教程和说明书页面**：在 `http://127.0.0.1:8737/` 和 `/manual` 提供，只回应来自这个地址的页面。页面用的字体随 padpilot 一起提供（`padpilot/fonts/`，SIL 开源字体协议），页面不从网上加载任何东西。

## 故障排查

- `padpilot doctor` 会逐项检查 hidapi、手柄、daemon。
- 日志在 `~/.padpilot/daemon.log` 和 `daemon.stderr`。
- macOS 下手柄只能被一个进程打开。Steam、DS4Windows 类工具或其他手柄映射软件占用手柄时，daemon 会打不开它。
- 蓝牙连接时，daemon 会读取一次 feature report 0x05，把手柄切到完整报告模式；拔掉 USB 后会自动重连。

## 测试

```bash
python3 -m unittest tests/test_core.py   # 协议、按键、灯效、状态机（不需要手柄）
python3 tests/drive_wrapper.py           # PTY 包装器 + 假 agent（需要 daemon）
python3 tests/drive_claude.py [kitty]    # 真实 Claude Code 交互端到端（消耗少量 token）
python3 tests/drive_codex.py             # 真实 Codex 交互端到端
```
