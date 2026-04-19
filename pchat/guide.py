from __future__ import annotations

GUIDE_VERSION = 1


FIRST_RUN_GUIDE_ZH = """
====================================
P-Chat 使用指南
====================================
1. 先确认大家连接到同一个 Wi-Fi。
2. 启动后程序会自动搜索房间。
3. 找到房间会自动加入；找不到房间时可以选择创建房间。
4. 直接输入文字即可发送聊天消息。
5. 常用命令：
   /help      查看命令
   /users     查看在线用户
   /history   查看聊天记录
   /sync      同步最近消息
   /undo      撤回自己最近一条消息
   /quit      退出程序

本程序的数据保存在 exe 同目录下的 .pchat 文件夹中。
删除 pc.exe 和 .pchat 文件夹即可清理程序。
====================================
""".strip()


FIRST_RUN_GUIDE_EN = """
====================================
P-Chat Quick Guide
====================================
1. Make sure everyone is on the same Wi-Fi.
2. P-Chat searches for a room automatically.
3. If a room is found, it joins automatically. If no room is found, you can create one.
4. Type text directly to send chat messages.
5. Common commands:
   /help      Show commands
   /users     Show online users
   /history   Show local chat history
   /sync      Sync recent messages
   /undo      Withdraw your latest message
   /quit      Quit
6. Host commands:
   /announce set <text>   Publish announcement
   /update publish <file> Publish update package

Program data is stored in the .pchat folder beside pc.exe.
Delete pc.exe and .pchat to clean up P-Chat.
====================================
""".strip()


def first_run_guide() -> str:
    return f"{FIRST_RUN_GUIDE_EN}\n\n{FIRST_RUN_GUIDE_ZH}"
