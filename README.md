# ccxp · Codex 与 Claude Code 会话配对

在同一个工作区绑定一个本地 Codex 聊天和一个 Claude Code 终端。普通发言时会同步对端新增的用户文字和最终回答。两个原生窗口仍然各自显示自己的聊天记录。

## 一次性设置

在同一台 Mac 上安装 Python 3.9+、Codex CLI 和 Claude Code，并登录两个客户端。选择一个长期保留的位置克隆本仓库，然后把它注册为两个客户端的个人 skill。

```sh
git clone https://github.com/kangchainx/ccxp.git
cd ccxp
mkdir -p "$HOME/.codex/skills" "$HOME/.claude/skills"
ln -s "$PWD" "$HOME/.codex/skills/ccxp"
ln -s "$PWD" "$HOME/.claude/skills/ccxp"
python3 pair.py install
```

首次运行安装命令会在两个客户端的用户配置中各追加一个 `UserPromptSubmit` hook。通过 Codex 的 `/hooks` 查看并信任命令末尾为 `pair.py hook codex` 的单个条目。

## 使用 skill

在 Codex 输入 `$ccxp`，或在 Claude Code 输入 `/ccxp`。助手会优先展示选择组件：

1. 创建新的对端会话并绑定。Codex 端启动同工作区的后台 Claude Code；Claude Code 端创建一个可恢复的 Codex 聊天，初始化时发送一条仅需回复「就绪」的消息。
2. 绑定已有的对端会话。选择后提供目标会话的完整 UUID。

也可以直接输入 `$ccxp 对方UUID` 或 `/ccxp 对方UUID` 跳过选择。选新会话会替换本端原配对。如果要绑定的现有对端已连着第三方，会先确认是否解除那一对。

## 让两边直接对话

绑定后，已交给两端协作的任务可由助手自行发送必要的交接和结论。也可以在 Claude Code 说「跟 Codex 说……」，或在 Codex 说「跟 Claude Code 说……」，当前模型会运行 `pair.py send --from 本端 --message '内容'`，把这句话作为新一轮消息投给对方，对方空闲时自动开始处理，需要回答时用同一条命令回话。

- 需要本轮使用 Claude 的回复时，Codex 运行 `pair.py listen --request "请评审……"`，监听就绪后才发出请求。已发送请求时直接运行 `pair.py listen`。Claude 沿用原来的发送命令；监听存续期间，同一配对的每条主动回信按到达顺序进入本轮工具输出。收到最终结论后结束监听。会话没有监听时仍走 `codex queue --resume`。这条通道不会移动桌面端已有的排队消息。
- 发往 Claude Code 由一个临时的无界面 Claude 会话转交（Claude Code 只接收来自 Claude 会话的消息）。脚本用配对 UUID 在 `claude agents --json` 中查出目标的当前名称，自动名称同样可用。目标会话需要正在运行，每次转交消耗少量额度。
- 投递的消息以 `[Claude Code → Codex]` 或 `[Codex → Claude Code]` 开头。

## 同步范围

- 对方原生记录是来源。在本端提交普通消息时，hook 增量读取对方已经落盘的记录，只把对方近期的可见文字放进本轮上下文。正在生成的答案会在对方的最终回复落盘后可用。
- 首次提交会收到投递说明及最近的对话摘录。之后每个方向分别记录同步进度；一般提交没有新增可见内容时不附加任何上下文。本端发生上下文压缩后，仅恢复一次投递和回查提示。主动发回本端的消息也会从回传摘录中排除。
- 每批摘录最多约 9,000 个 UTF-8 字节，另附简短的说明。多条长消息会优先展示最新内容，并提供按序号取回原文的命令。一次钩子成功输出后即记录进度；客户端中断或压缩历史后，可在当前工作区运行 `./pair.py history --peer-of codex --limit 5`（Claude 端用 `claude`）。用 `--after N`、`--through N` 和 `--limit N` 分页；区间左开右闭。`./pair.py history SESSION_ID` 仍可读取完整已索引原文。
- 主动消息仍然立即走原生投递通道。后台聊天记录仅在下次提交时补充，不会自行唤醒对端。图片、工具输出和推理过程不参与同步。
- 配对、文件读取位置、同步进度和已提取文字保存在当前用户的 `~/.local/share/codex-claude-pair/pairs.sqlite3`。hook 只对匹配的会话 ID 和工作目录生效。
- `./pair.py unlink THREAD_ID` 解除配对并清除这对会话的本地索引。两个客户端的原生会话历史仍由各自管理。
- 当前读取器针对 Codex CLI 0.169.1 和 Claude Code 2.1.329 的本地 JSONL 记录。升级客户端后可以用两个只在各自一端出现的随机词做一次往返检查。

主动交接也自动压缩。助手先整理要点，脚本在投递前检查正文，超过 9,000 个 UTF-8 字节时仅投递首尾摘录与缺失区间的读取命令。接收方可按需自行分页读取，解绑会清理该配对存储的原文。

发往 Claude Code 的超长交接通过短引用转递，接收方收到后立即执行该引用读取首尾摘录，再根据任务自行补取缺失的约束。这样中继无需重写摘录。发往 Codex 的摘录直接随消息送达。


## 数据与隐私

配对后，近期用户消息和最终答复会进入另一端的模型上下文。主动交接也会通过你的 Codex 或 Claude Code 客户端发送。SQLite 索引及长消息原文存放在本机用户目录；配对的会话可以按需读取相关内容。请按工作区的数据共享要求选择配对对象。

## 测试与许可

运行 `python3 -m unittest -v` 可在临时目录里验证回信监听、配对隔离和队列切换。项目采用 [MIT 许可证](LICENSE)。
