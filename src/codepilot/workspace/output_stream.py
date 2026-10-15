"""通过有界 JSON 帧把容器原始输出逐块传给控制器私有存储。"""

import asyncio
import base64
import hashlib
import json
import os
import resource

from codepilot.sessions.outputs import Capture
from codepilot.tools.process import settle, spawn, stop_process
from codepilot.workspace.common import read_limited


class FrameCapture:
    """容器只传输原始字节，不在临时盘聚合整个命令输出。"""

    def __init__(self, output_id: str) -> None:
        """保存控制器分配的 opaque ID，让结果中的引用一致。"""
        self.output_id = output_id
        self.state = "partial"
        self.counts = dict.fromkeys(("stdout", "stderr"), 0)
        self.hashes = {name: hashlib.sha256() for name in self.counts}

    def begin(self, call_id: str) -> "FrameCapture":
        """当前 worker 只处理一次命令，复用这一条帧通道。"""
        return self

    def write(self, stream: str, data: bytes) -> None:
        """同步刷新有限帧，由管道背压限制生产速度。"""
        self.counts[stream] += len(data)
        self.hashes[stream].update(data)
        print(
            json.dumps(
                {"frame": "chunk", "stream": stream, "data": base64.b64encode(data).decode()}
            ),
            flush=True,
        )

    def finish(self, state: str) -> None:
        """发布管道完整性，最终结果缺失时控制器仍按 partial 处理。"""
        self.state = state
        print(
            json.dumps(
                {
                    "frame": "capture_finished",
                    "state": state,
                    "bytes": self.counts,
                    "sha256": {name: digest.hexdigest() for name, digest in self.hashes.items()},
                    "worker_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                }
            ),
            flush=True,
        )

    def description(self) -> str:
        """工具结果只返回随机 ID 与读取方法，不返回宿主路径。"""
        return (
            f"output_id={self.output_id}\noutput_state={self.state}\n"
            "可用 read_output/search_output 读搜原始输出。"
        )


async def stream_worker(
    arguments: tuple[str, ...], payload: bytes, capture: Capture
) -> tuple[int, bytes, bytes]:
    """消费分块与单个最终结果；传输、配额、取消失败都回收 Docker CLI。"""
    process = await spawn(
        ("docker", *arguments),
        cwd=os.getcwd(),
        stdin=True,
        stream_limit=16 * 1024**2,
        capture=capture,
    )
    assert process.stdin is not None and process.stdout is not None
    error_reader = asyncio.create_task(read_limited(process.stderr, 65536))
    state = "partial"
    result: bytes | None = None
    remote_state = "partial"
    finished = False
    try:
        process.stdin.write(payload)
        await process.stdin.drain()
        process.stdin.close()
        while line := await process.stdout.readline():
            frame = json.loads(line)
            kind = frame.get("frame")
            # 正文只在完成帧以前接收，重复或乱序帧不拼接成可信输出。
            if kind == "chunk":
                # 每帧上限独立于日志总大小，避免恶意 worker 强迫控制器分配巨大块。
                if (
                    finished
                    or result is not None
                    or len(line) > 32768
                    or frame["stream"] not in {"stdout", "stderr"}
                ):
                    raise ValueError("invalid output frame")
                capture.write(frame["stream"], base64.b64decode(frame["data"], validate=True))
            elif kind == "capture_finished":
                if (
                    finished
                    or result is not None
                    or frame["state"] not in {"complete", "partial", "quota_exceeded"}
                ):
                    raise ValueError("invalid capture completion")
                finished = True
                remote_state = frame["state"]
                hashes = getattr(capture, "hashes", {})
                if frame["bytes"] != getattr(capture, "saved", {}) or frame["sha256"] != {
                    name: digest.hexdigest() for name, digest in hashes.items()
                }:
                    raise ValueError("output stream integrity mismatch")
                metrics = getattr(capture, "metrics", None)
                if metrics is not None:
                    metrics.update(
                        worker_peak_rss_kib=int(frame["worker_peak_rss_kib"]),
                        controller_peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                    )
            elif kind == "result" and result is None:
                result = json.dumps(frame["result"]).encode()
            else:
                raise ValueError("invalid worker frame order")
        await process.wait()
        stderr, _ = await error_reader
        if process.returncode == 0 and result is None:
            raise ValueError("worker final result missing")
        state = remote_state if process.returncode == 0 else "partial"
        return process.returncode or 0, result or b"", stderr
    except BaseException:
        # Docker CLI 停止后，上层还必须停止容器内执行，二者不是同一生命周期。
        await settle(stop_process(process))
        error_reader.cancel()
        await settle(asyncio.gather(error_reader, return_exceptions=True))
        raise
    finally:
        capture.finish(state)
