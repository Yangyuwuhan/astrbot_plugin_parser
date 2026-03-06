import os
from itertools import chain
from pathlib import Path

from astrbot.core.message.components import (
    BaseMessageComponent,
    File,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Video,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .config import PluginConfig
from .data import (
    AudioContent,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    ParseResult,
    VideoContent,
)
from .exception import (
    DownloadException,
    DownloadLimitException,
    SizeLimitException,
    ZeroSizeException,
)
from .render import Renderer


class MessageSender:
    """
    消息发送器

    职责：
    - 根据解析结果（ParseResult）规划发送策略
    - 控制是否渲染卡片、是否强制合并转发
    - 将不同类型的内容转换为 AstrBot 消息组件并发送

    重要原则：
    - 不在此处做解析
    - 不在此处决定“内容是什么”
    - 只负责“怎么发”
    """

    def __init__(self, config: PluginConfig, renderer: Renderer):
        self.cfg = config
        self.renderer = renderer

    def _get_windows_path(self, path: Path) -> str:
        """返回 Windows 可访问的路径（正斜杠形式）"""
        return path.as_posix()

    def _get_wsl2_path(self, path: Path) -> str:
        """
        将 Windows 路径转换为 WSL2 可访问的路径字符串。
        例如：F:\\AstrBotLauncher\\... → /mnt/f/AstrBotLauncher/...
        """
        path_str = str(path)
        drive, tail = os.path.splitdrive(path_str)
        if drive:
            drive_letter = drive[0].lower()
            tail = tail.lstrip("\\").replace("\\", "/")
            return f"/mnt/{drive_letter}/{tail}"
        else:
            # 没有盘符的情况（如网络路径），直接转换斜杠
            return path_str.replace("\\", "/")

    def _build_send_plan(self, result: ParseResult) -> dict:
        """
        根据解析结果生成发送计划（plan）

        plan 只做“策略决策”，不做任何 IO 或发送动作。
        后续发送流程严格按 plan 执行，避免逻辑分散。
        """
        light, heavy = [], []

        # 合并主内容 + 转发内容，统一参与发送策略计算
        for cont in chain(
            result.contents, result.repost.contents if result.repost else ()
        ):
            match cont:
                case ImageContent() | GraphicsContent():
                    light.append(cont)
                case VideoContent() | AudioContent() | FileContent() | DynamicContent():
                    heavy.append(cont)
                case _:
                    light.append(cont)

        # 仅在“单一重媒体且无其他内容”时，才允许渲染卡片
        is_single_heavy = len(heavy) == 1 and not light
        render_card = is_single_heavy and self.cfg.single_heavy_render_card
        # 实际消息段数量（卡片也算一个段）
        seg_count = len(light) + len(heavy) + (1 if render_card else 0)

        # 达到阈值后，强制合并转发，避免刷屏
        force_merge = seg_count >= self.cfg.forward_threshold

        return {
            "light": light,
            "heavy": heavy,
            "render_card": render_card,
            # 预览卡片：仅在“渲染卡片 + 不合并”时独立发送
            "preview_card": render_card and not force_merge,
            "force_merge": force_merge,
        }

    async def _send_preview_card(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        plan: dict,
    ):
        """
        发送预览卡片（独立消息）

        场景：
        - 只有一个重媒体
        - 未触发合并转发
        - 卡片作为“预览”，不与正文混合
        """
        if not plan["preview_card"]:
            return

        if image_path := await self.renderer.render_card(result):
            # 预览卡片是图片，使用 Windows 路径（AstrBot 会将其转为 base64 发送）
            path_str = self._get_windows_path(image_path)
            await event.send(event.chain_result([Image(path_str)]))

    async def _build_segments(
        self,
        result: ParseResult,
        plan: dict,
    ) -> list[BaseMessageComponent]:
        """
        根据发送计划构建消息段列表

        这里负责：
        - 下载媒体
        - 转换为 AstrBot 消息组件
        """
        segs: list[BaseMessageComponent] = []

        # 合并转发时，卡片以内联形式作为一个消息段参与合并
        if plan["render_card"] and plan["force_merge"]:
            if image_path := await self.renderer.render_card(result):
                # 卡片图片使用 Windows 路径
                path_str = self._get_windows_path(image_path)
                segs.append(Image(path_str))

        # 轻媒体处理
        for cont in plan["light"]:
            try:
                path: Path = await cont.get_path()
            except (DownloadLimitException, ZeroSizeException):
                continue
            except DownloadException:
                if self.cfg.show_download_fail_tip:
                    segs.append(Plain("此项媒体下载失败"))
                continue

            match cont:
                case ImageContent():
                    # 图片：使用 Windows 路径，让 AstrBot 转为 base64
                    path_str = self._get_windows_path(path)
                    segs.append(Image(path_str))
                case GraphicsContent() as g:
                    # GraphicsContent 本质也是图片，使用 Windows 路径
                    path_str = self._get_windows_path(path)
                    segs.append(Image(path_str))
                    # 携带补充文本
                    if g.text:
                        segs.append(Plain(g.text))
                    if g.alt:
                        segs.append(Plain(g.alt))

        # 重媒体处理
        for cont in plan["heavy"]:
            try:
                path: Path = await cont.get_path()
            except SizeLimitException:
                segs.append(Plain("此项媒体超过大小限制"))
                continue
            except DownloadException:
                if self.cfg.show_download_fail_tip:
                    segs.append(Plain("此项媒体下载失败"))
                continue

            match cont:
                case VideoContent() | DynamicContent():
                    # 视频：需要 Napcat 直接读取文件，因此使用 WSL2 路径（如果配置）
                    if self.cfg.napcat_platform == "wsl2":
                        path_str = self._get_wsl2_path(path)
                    else:
                        path_str = self._get_windows_path(path)
                    segs.append(Video(path_str))
                case AudioContent():
                    # 音频：根据配置决定以文件还是语音形式发送
                    if self.cfg.napcat_platform == "wsl2":
                        path_str = self._get_wsl2_path(path)
                    else:
                        path_str = self._get_windows_path(path)
                    if self.cfg.audio_to_file:
                        segs.append(File(name=path.name, file=path_str))
                    else:
                        segs.append(Record(path_str))
                case FileContent():
                    # 文件：使用 WSL2 路径（如果配置）
                    if self.cfg.napcat_platform == "wsl2":
                        path_str = self._get_wsl2_path(path)
                    else:
                        path_str = self._get_windows_path(path)
                    segs.append(File(name=path.name, file=path_str))

        return segs

    def _merge_segments_if_needed(
        self,
        event: AstrMessageEvent,
        segs: list[BaseMessageComponent],
        force_merge: bool,
    ) -> list[BaseMessageComponent]:
        """
        根据策略决定是否将消息段合并为转发节点

        合并后的消息结构：
        - 每个原始消息段成为一个 Node
        - 统一使用机器人自身身份
        """
        if not force_merge or not segs:
            return segs

        nodes = Nodes([])
        self_id = event.get_self_id()

        for seg in segs:
            nodes.nodes.append(Node(uin=self_id, name="有曦-解析", content=[seg]))

        return [nodes]

    async def send_parse_result(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
    ):
        """
        发送解析结果的统一入口

        执行顺序固定：
        1. 构建发送计划
        2. 发送预览卡片（如有）
        3. 构建消息段
        4. 必要时合并转发
        5. 最终发送
        """
        plan = self._build_send_plan(result)

        await self._send_preview_card(event, result, plan)

        segs = await self._build_segments(result, plan)
        segs = self._merge_segments_if_needed(event, segs, plan["force_merge"])

        if segs:
            await event.send(event.chain_result(segs))
