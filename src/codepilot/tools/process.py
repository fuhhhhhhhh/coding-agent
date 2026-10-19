"""统一子进程排空、输出捕获和不可被二次取消打断的资源回收。"""

import asyncio
import os
import signal
from collections.abc import Awaitable, Mapping
from typing import TypeVar

from codepilot.sessions.outputs import CURRENT_OUTPUT, Capture, OutputLimitError

T = TypeVar("T")


async def settle(awaitable: Awaitable[T]) -> T:
    """等待清理真正结束；重复取消只延迟传播，不遗弃后台清理任务。"""
    task = asyncio.ensure_future(awaitable)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # 清理本身取消时不得无限重试；外部取消则继续等待确定收尾。
            if task.done():
                return task.result()


async def stop_process(process: asyncio.subprocess.Process) -> None:
    """即使组长已退出仍终止同组后代，并回收直接子进程。"""
    try:
        # 组长退出不表示继承管道的后代退出，POSIX 仍发送组信号。
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        elif process.returncode is None:
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), 0.5)
    except TimeoutError:
        pass
    # 后代可能忽略 SIGTERM，不能仅因直接进程已退出就省略强制组清理。
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()
    except ProcessLookupError:
        pass
    await process.wait()


async def spawn(
    command: tuple[str, ...],
    *,
    cwd: str,
    shell: bool = False,
    stdin: bool = False,
    stream_limit: int = 65536,
    capture: Capture | None = None,
    env: Mapping[str, str] | None = None,
) -> asyncio.subprocess.Process:
    """把创建进程的取消窗口纳入归属管理，避免启动完成却丢失句柄。"""
    operation = (
        asyncio.create_subprocess_shell(
            command[0],
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE if stdin else None,
            limit=stream_limit,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
        if shell
        else asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE if stdin else None,
            limit=stream_limit,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
    )
    task = asyncio.create_task(operation)
    try:
        process = await asyncio.shield(task)
        factory = CURRENT_OUTPUT.get()
        callback = getattr(factory, "on_process", None)
        if callback is not None:
            try:
                callback(process.pid)
            except BaseException:
                await settle(stop_process(process))
                raise
        return process
    except asyncio.CancelledError:
        # 创建已经交给操作系统，必须取得句柄后杀掉，而不是取消创建协程。
        process = await settle(task)
        await settle(stop_process(process))
        if capture is not None:
            capture.finish("partial")
        raise
    except BaseException:
        if capture is not None:
            capture.finish("partial")
        raise


async def drain(
    stream: asyncio.StreamReader | None, limit: int, capture: Capture | None, name: str
) -> tuple[bytes, bool]:
    """先捕获原始字节再生成有限预览；任何存储异常立即向上层传播。"""
    preview = bytearray()
    total = 0
    if stream is not None:
        while chunk := await stream.read(8192):
            if capture is not None:
                capture.write(name, chunk)
            total += len(chunk)
            preview.extend(chunk[: max(0, limit - len(preview))])
    return bytes(preview), total > limit


async def collect(
    process: asyncio.subprocess.Process, limit: int, call_id: str, capture: Capture | None = None
) -> tuple[bytes, bool, bytes, bool, str]:
    """并行排空两路与等待退出，任一路失败都停止命令并提交不完整输出。"""
    readers = [
        asyncio.create_task(drain(process.stdout, limit, capture, "stdout")),
        asyncio.create_task(drain(process.stderr, limit, capture, "stderr")),
    ]
    waiting = asyncio.create_task(process.wait())
    state = "complete"
    try:
        await asyncio.gather(*readers, waiting)
        stdout, out_cut = readers[0].result()
        stderr, err_cut = readers[1].result()
    except BaseException as error:
        # 失败后先停止进程，读管道任务即使已经失败也必须统一收敛。
        state = "quota_exceeded" if isinstance(error, OutputLimitError) else "partial"
        await settle(stop_process(process))
        for task in (*readers, waiting):
            if not task.done() and not task.cancelling():
                task.cancel()
        await settle(asyncio.gather(*readers, waiting, return_exceptions=True))
        raise
    finally:
        if capture is not None:
            capture.finish(state)
    return stdout, out_cut, stderr, err_cut, capture.description() if capture else ""
