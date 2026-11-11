# SPDX-FileCopyrightText: 2026-present cqian-cs <cqian.cs@qq.com>
#
# SPDX-License-Identifier: MIT
"""besh - Beacon Shell.

把闲置算力（手机 Termux、免费云 IDE、云 NoteBook）变成可远程操作的工人。

四个角色：

* 信标（beacon）—— 有公网 IP 的服务器，中转命令、记录状态、提供 Web 界面与 MCP。
* 工人（worker）—— 任意终端，向信标轮询命令并交给 tmux 执行。
* 遥控器（controller）—— 本地电脑，用 tmux 风格的命令行操作多个工人。
* 对象存储（R2）—— 备份工人的工作目录。
"""

from .__about__ import __version__

__all__ = ["__version__"]
