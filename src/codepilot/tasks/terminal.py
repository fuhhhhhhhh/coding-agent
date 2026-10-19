"""有界 PTY 输入输出，秘密输入不回显，取消时回收精确归属进程组。"""

import asyncio
import errno
import fcntl
import os
import signal
import struct
import termios
from collections.abc import Callable
from pathlib import Path

from codepilot.sessions.outputs import CURRENT_OUTPUT
from codepilot.tools.process import settle, stop_process


class TerminalProcess:
    """保存当前任务的 PTY 与进程句柄，不允许通过外部 PID 注入输入。"""

    def __init__(self) -> None:
        """终端仅在经过审批的任务启动时创建。"""
        self.master: int | None = None
        self.process: asyncio.subprocess.Process | None = None

    def input(self, data: str, *, eof: bool = False, rows: int = 24, columns: int = 80) -> None:
        """有界输入并设置窗口尺寸；输入正文不写入任务日志或 Trace。"""
        if self.master is None:
            raise ValueError("interactive task is not running")
        if len(data.encode()) > 4096 or not 1 <= rows <= 200 or not 1 <= columns <= 500:
            raise ValueError("terminal input or dimensions exceed bound")
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        if self.process is not None and self.process.returncode is None:
            os.killpg(self.process.pid, signal.SIGWINCH)
        os.write(self.master, b"\x04" if eof else data.encode())

    async def run(
        self, argv: tuple[str, ...], root: Path, call_id: str, on_chunk: Callable[[str], None]
    ) -> str:
        """使用非阻塞 PTY 排空输出，捕获配额失败时停止整个进程组。"""
        master, slave = os.openpty()
        self.master = master
        os.set_blocking(master, False)
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        settings = termios.tcgetattr(slave)
        settings[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, settings)
        factory = CURRENT_OUTPUT.get()
        capture = factory.begin(call_id) if factory is not None else None
        process = None
        complete = False
        try:
            creation = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *argv,
                    cwd=root,
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    start_new_session=True,
                    env={
                        key: value
                        for key, value in os.environ.items()
                        if key
                        in {
                            "PATH",
                            "HOME",
                            "LANG",
                            "XDG_RUNTIME_DIR",
                            "DOCKER_HOST",
                            "DOCKER_CONTEXT",
                        }
                    },
                )
            )
            try:
                process = await asyncio.shield(creation)
            except asyncio.CancelledError:
                # 创建已交给操作系统，取消时先取得句柄，再由 finally 回收。
                process = await settle(creation)
                raise
            self.process = process
            callback = getattr(factory, "on_process", None)
            if callback is not None:
                callback(process.pid)
            os.close(slave)
            slave = -1
            loop = asyncio.get_running_loop()
            while True:
                ready: asyncio.Future[None] = loop.create_future()

                def readable(ready: asyncio.Future[None] = ready) -> None:
                    """以 Future 唤醒单次读等待，避免工作线程被 PTY 阻塞。"""
                    if not ready.done():
                        ready.set_result(None)

                loop.add_reader(master, readable)
                try:
                    await ready
                    try:
                        chunk = os.read(master, 8192)
                    except OSError as error:
                        if error.errno == errno.EIO:
                            break
                        raise
                finally:
                    loop.remove_reader(master)
                if not chunk:
                    break
                if capture is not None:
                    capture.write("stdout", chunk)
                on_chunk(chunk.decode("utf-8", errors="replace"))
            await process.wait()
            complete = True
            return f"exit_code={process.returncode}\n" + (capture.description() if capture else "")
        finally:
            # 不在创建后遗弃进程；双重取消也必须等到进程组停止后再释放 PTY。
            if process is not None:
                await settle(stop_process(process))
            if slave >= 0:
                os.close(slave)
            os.close(master)
            self.master = None
            self.process = None
            if capture is not None:
                capture.finish("complete" if complete else "partial")
