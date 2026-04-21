from __future__ import annotations

GUIDE_VERSION = 3


FIRST_RUN_GUIDE_ZH = """
====================================
P-Chat 使用指南
====================================
1. 先确认大家连接在同一局域网，或附近有这台电脑保存过的 Wi-Fi。
2. 启动后程序会先在当前 Wi-Fi 上自动搜索房间。
3. 如果当前 Wi-Fi 没有房间，你可以：
   - 遍历当前可见且系统已保存的 Wi-Fi
   - 手动选择一个 Wi-Fi 再扫描
   - 跳过切换 Wi-Fi
4. 找到房间后会自动加入；如果一直找不到，可以自己创建房间。
5. 直接输入文字并回车即可发送消息。
6. 常用命令：
   /help           查看命令
   /users          查看在线用户
   /history        查看聊天记录
   /sync           同步最近消息
   /wifi           查看当前 Wi-Fi 和候选 Wi-Fi
   /translate on   开启中文到日文翻译
   /translate off  关闭翻译
   /quit           退出程序

默认会连接 LibreTranslate 公共实例：
- https://zh.libretranslate.com/
- 如需 API key，可用 /translate key <API_KEY> 保存本机 key

程序数据保存在 pc.exe 同目录下的 .pchat 文件夹中。
删除 pc.exe 和 .pchat 即可清理程序数据。
====================================
""".strip()


FIRST_RUN_GUIDE_EN = """
====================================
P-Chat Quick Guide
====================================
1. Make sure you are on the same LAN, or near saved Wi-Fi networks that this PC already knows.
2. P-Chat searches the current Wi-Fi for a room first.
3. If no room is found, you can:
   - scan saved and visible Wi-Fi networks
   - choose one Wi-Fi manually and scan it
   - skip Wi-Fi switching
4. If a room is found, P-Chat joins automatically. If none is found, you can create one.
5. Type text directly to send chat messages.
6. Common commands:
   /help             Show commands
   /users            Show online users
   /history          Show local chat history
   /sync             Sync recent messages
   /wifi             Show current Wi-Fi and saved visible candidates
   /translate on     Enable Chinese -> Japanese translation
   /translate off    Disable translation
   /quit             Quit

LibreTranslate public instance is used by default:
- https://zh.libretranslate.com/
- If your instance requires a key, run /translate key <API_KEY>

Program data is stored in the .pchat folder beside pc.exe.
Delete pc.exe and .pchat to clean up P-Chat.
====================================
""".strip()


def first_run_guide() -> str:
    return f"{FIRST_RUN_GUIDE_EN}\n\n{FIRST_RUN_GUIDE_ZH}"
