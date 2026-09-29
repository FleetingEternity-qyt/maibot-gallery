import os
import re
import json
import time
import base64
import hashlib
import asyncio
import random
from io import BytesIO
from typing import Any, ClassVar

from PIL import Image
from maibot_sdk import (
    MaiBotPlugin, PluginConfigBase, Field,
    Command, HookHandler, Tool
)
from maibot_sdk.types import (
    HookMode, HookOrder, ErrorPolicy, ToolParamType, ToolParameterInfo
)

SUPPORTED_CONFIG_VERSION = "1.0.0"
COOLDOWN_SECONDS = 24 * 3600


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "image"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    admin_qq: str = Field(default="", description="管理员QQ号（用于接收私聊投喂和暗号）")
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本",
        json_schema_extra={"hidden": True, "disabled": True}
    )


class ImageGalleryConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)


class ImageGalleryPlugin(MaiBotPlugin):
    config_model: ClassVar[type[PluginConfigBase] | None] = ImageGalleryConfig

    async def on_load(self) -> None:
        self.ctx.logger.info("麦麦的图库插件已加载，enabled=%s", self.config.plugin.enabled)
        
        self.data_dir = self.ctx.paths.data_dir
        self.image_dir = self.data_dir / "images"
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.index_file = self.data_dir / "index.json"
        self.pending_file = self.data_dir / "pending_requests.json"
        
        if self.index_file.exists():
            self.index = json.loads(self.index_file.read_text(encoding="utf-8"))
        else:
            self.index = {"images": {}, "aliases": {}}
            self._save_index()
            
        if self.pending_file.exists():
            self.pending_requests = json.loads(self.pending_file.read_text(encoding="utf-8"))
        else:
            self.pending_requests = {}
            self._save_pending()
            
        # 内存临时状态：等待管理员发图的标签暂存
        self.pending_upload = {}  # {admin_qq: {"tags": [...], "time": 时间戳}}
        self.index_lock = asyncio.Lock()
        
        self.ctx.logger.info("图库加载完毕，当前共有 %d 张图片", len(self.index.get("images", {})))

    async def on_unload(self) -> None:
        self.ctx.logger.info("图库插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope == "self":
            self.ctx.logger.info("配置已热更新 version=%s", version)

    def _save_index(self):
        self.index_file.write_text(json.dumps(self.index, ensure_ascii=False, indent=2), encoding="utf-8")

    def _save_pending(self):
        self.pending_file.write_text(json.dumps(self.pending_requests, ensure_ascii=False, indent=2), encoding="utf-8")

    def _save_image_file(self, img_base64: str) -> tuple[str, str]:
        if "," in img_base64:
            img_base64 = img_base64.split(",")[1]
        img_hash = hashlib.sha256(img_base64.encode("utf-8")).hexdigest()[:16]
        filename = f"{img_hash}.jpg"
        file_path = self.image_dir / filename
        
        if not file_path.exists():
            try:
                img_data = base64.b64decode(img_base64)
                img = Image.open(BytesIO(img_data)).convert("RGB")
                if img.width > 1080:
                    ratio = 1080 / img.width
                    img = img.resize((1080, int(img.height * ratio)), Image.Resampling.LANCZOS)
                img.save(file_path, "JPEG", quality=85)
            except Exception as e:
                self.ctx.logger.error(f"图片保存失败: {e}")
                raise e
        return img_hash, filename

    async def _generate_tags_by_vlm(self, img_base64: str) -> dict:
        clean_base64 = img_base64.split(",")[1] if "," in img_base64 else img_base64
        prompt = (
            "你是一个摄影助理。请仔细观察这张图片，提取3-5个简短的名词标签，"
            "并写一句简短的口语化描述。严格以JSON对象格式返回，不要包含任何多余的文字。\n"
            "示例输出：{\"tags\": [\"日落\", \"风景\"], \"description\": \"傍晚拍到的日落\"}"
        )
        try:
            start_time = time.time()
            response = await self.ctx.llm.generate(
                prompt=[{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{clean_base64}"}}
                ]}],
                model="vlm", temperature=0.3, max_tokens=200
            )
            self.ctx.logger.info(f"VLM 识别完成，耗时 {time.time() - start_time:.2f} 秒")
            if response.get("success"):
                raw_text = response["response"].strip().replace("```json", "").replace("```", "").strip()
                result = json.loads(raw_text)
                return {"tags": result.get("tags", []), "description": result.get("description", "")}
        except Exception as e:
            self.ctx.logger.error(f"VLM识别图片失败: {e}")
        return {"tags": [], "description": ""}

    # ==========================================
    # 核心：拦截管理员私聊图片
    # ==========================================
    @HookHandler(
        "chat.receive.before_process",
        name="capture_admin_image",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP
    )
    async def capture_admin_image(self, message: dict, **kwargs):
        if not self.config.plugin.enabled or not message:
            return {"action": "continue"}

        user_id = str(message.get("message_info", {}).get("user_info", {}).get("user_id", ""))
        admin_qq = str(self.config.plugin.admin_qq)
        if user_id != admin_qq:
            return {"action": "continue"}

        raw_message = message.get("raw_message", [])
        stream_id = kwargs.get("stream_id", "")
        
        # 过滤掉纯指令消息（交给 @Command 处理）
        has_command = any(seg.get("type") == "text" and seg.get("data", "").startswith("/") for seg in raw_message)
        if has_command:
            return {"action": "continue"}

        for segment in raw_message:
            if segment.get("type") in ("image", "emoji"):
                img_base64 = segment.get("binary_data_base64") or segment.get("data")
                if not img_base64:
                    continue

                self.ctx.logger.info("收到管理员图片，准备处理...")
                try:
                    img_hash, filename = self._save_image_file(img_base64)
                except Exception:
                    await self.ctx.send.text("图片处理失败了，换个格式再试试？", stream_id)
                    return {"action": "abort"}

                # 检查是否有待处理的手动入库指令
                if admin_qq in self.pending_upload:
                    pending = self.pending_upload[admin_qq]
                    if time.time() - pending["time"] < 60:
                        tags = pending["tags"]
                        del self.pending_upload[admin_qq]  # 清除状态
                        
                        async with self.index_lock:
                            self.index["images"][img_hash] = {
                                "filename": filename, "tags": tags, "description": "管理员手动录入",
                                "created_at": int(time.time()), "last_sent_at": 0, "sent_count": 0
                            }
                            self._save_index()
                        
                        await self.ctx.send.text(f"已手动入库！标签：【{', '.join(tags)}】，图片ID：{img_hash}", stream_id)
                        return {"action": "abort"}
                    else:
                        del self.pending_upload[admin_qq]  # 超时清除

                # 如果是全自动流程，走 VLM 识别
                await self.ctx.send.text("收到图片啦，麦麦正在努力看...", stream_id)
                asyncio.create_task(self._async_process_and_reply(img_base64, img_hash, filename, stream_id))
                return {"action": "abort"}
        return {"action": "continue"}

    async def _async_process_and_reply(self, img_base64: str, img_hash: str, filename: str, stream_id: str):
        try:
            vlm_result = await self._generate_tags_by_vlm(img_base64)
            tags = vlm_result["tags"]
            description = vlm_result["description"]

            if not tags:
                await self.ctx.send.text("我努力看了好久，还是没看清这张图。请回复 /add_image 标签1 标签2 再发一次给我。", stream_id)
                return

            async with self.index_lock:
                self.index["images"][img_hash] = {
                    "filename": filename, "tags": tags, "description": description,
                    "created_at": int(time.time()), "last_sent_at": 0, "sent_count": 0
                }
                self._save_index()

            await self._check_and_fulfill_pending_requests(tags, img_base64)
            await self.ctx.send.text(f"【自动入库】麦麦认出这是【{', '.join(tags)}】\n描述：{description}\n如果认错了，回复 `/edit_tag {img_hash} 新标签` 告诉我哦！", stream_id)
        except Exception as e:
            self.ctx.logger.error(f"后台处理图片出错: {e}")
            await self.ctx.send.text("哎呀，刚才处理图片的时候出了点小差错，你再发一次试试？", stream_id)

    async def _check_and_fulfill_pending_requests(self, new_tags: list[str], img_base64: str):
        fulfilled = []
        for req_id, req in list(self.pending_requests.items()):
            if req["tag"] in new_tags or any(t in new_tags for t in req["tag"].split()):
                fulfilled.append(req)
                
        for req in fulfilled:
            async def delayed_send(group_stream_id=req["group_stream_id"], tag=req["tag"]):
                await asyncio.sleep(random.randint(5, 15))
                try:
                    await self.ctx.send.image(img_base64, group_stream_id)
                    await self.ctx.send.text(f"刚去相机里导出了一张，看看这张{tag}怎么样！", group_stream_id)
                except Exception as e:
                    self.ctx.logger.error(f"延迟补发图片失败: {e}")
            asyncio.create_task(delayed_send())
            del self.pending_requests[req["id"]]
            self._save_pending()

    # ==========================================
    # 群聊工具 & 状态联动
    # ==========================================
    @Tool(
        "send_gallery_image",
        brief_description="发送麦麦自己拍的照片",
        detailed_description="当群友想看某种照片（如日落、猫猫）时调用。如果图库没图或者今天发过了，你会收到系统提示，请用懊恼或无奈的语气告诉群友你还没拍到，绝对不能说'图库没有'、'管理员'这种词汇。",
        parameters=[ToolParameterInfo(name="tag", param_type=ToolParamType.STRING, description="图片类别关键词", required=True)]
    )
    async def tool_send_gallery_image(self, tag: str, **kwargs):
        stream_id = kwargs.get("stream_id")
        group_id = kwargs.get("group_id")
        if not stream_id: return {"content": "系统错误：未获取到聊天流ID"}

        # 状态联动
        if group_id:
            status = await self._get_bot_current_status(group_id, stream_id)
            if status:
                await self.ctx.maisaka.context.append(
                    stream_id=stream_id, segments=[{"type": "text", "content": f"【系统提示：我现在的状态是“{status}”，我正在做这件事。】"}]
                )

        target_tag = self.index.get("aliases", {}).get(tag, tag)
        now = time.time()
        matched_images = []
        has_tag_but_cooling = False
        
        for img_hash, img_info in self.index.get("images", {}).items():
            if target_tag in img_info.get("tags", []) or tag in img_info.get("tags", []):
                if img_info.get("last_sent_at", 0) > 0 and (now - img_info["last_sent_at"]) < COOLDOWN_SECONDS:
                    has_tag_but_cooling = True
                    continue
                matched_images.append(img_hash)

        if not matched_images:
            if has_tag_but_cooling:
                return {"content": "图库里没有新图了，请用无奈的语气告诉群友：今天的这张照片我已经发过了，等明天我再拍新的一张给你看。"}
            else:
                await self._request_image_from_admin(tag, group_id, stream_id)
                return {"content": "图库里没有这张图。请用懊恼的语气告诉群友：你还没拍到，等下再去拍拍看。"}

        chosen_hash = random.choice(matched_images)
        chosen_img = self.index["images"][chosen_hash]
        file_path = self.image_dir / chosen_img["filename"]
        if not file_path.exists(): return {"content": "图片文件丢失了，请告诉群友相机出了点故障。"}

        try:
            with open(file_path, "rb") as f: img_base64 = base64.b64encode(f.read()).decode("utf-8")
            await self.ctx.send.image(img_base64, stream_id)
            async with self.index_lock:
                self.index["images"][chosen_hash]["last_sent_at"] = int(now)
                self.index["images"][chosen_hash]["sent_count"] += 1
                self._save_index()
            return {"content": f"照片已成功发送到群里。你的照片描述是：{chosen_img.get('description', '')}。请用自然、得意的语气附带一句话。"}
        except Exception as e:
            self.ctx.logger.error(f"发送图库图片失败: {e}")
            return {"content": "发送图片时出了点故障，请向群友道歉。"}

    async def _request_image_from_admin(self, tag: str, group_id: str, group_stream_id: str):
        admin_qq = str(self.config.plugin.admin_qq)
        if not admin_qq: return
        try:
            session = await self.ctx.chat.open_session(platform="qq", chat_type="private", user_id=admin_qq)
            admin_stream_id = session["stream_id"]
            req_id = f"{group_id}_{tag}_{int(time.time())}"
            self.pending_requests[req_id] = {"id": req_id, "group_id": group_id, "group_stream_id": group_stream_id, "tag": tag, "time": time.time()}
            self._save_pending()
            await self.ctx.send.text(f"【暗号】群 {group_id} 的群友想看【{tag}】。我图库没了，请喂我一张。发图后我会自动补发到群里。", admin_stream_id)
        except Exception as e:
            self.ctx.logger.error(f"私聊管理员索图失败: {e}")

    async def _get_bot_current_status(self, group_id: str, stream_id: str) -> str:
        if not group_id: return ""
        try:
            login_resp = await self.ctx.api.call("adapter.napcat.system.get_login_info")
            if not isinstance(login_resp, dict) or login_resp.get("status") != "ok": return ""
            bot_qq = str(login_resp.get("data", {}).get("user_id", ""))
            resp = await self.ctx.api.call("adapter.napcat.group.get_group_member_info", group_id=int(group_id), user_id=int(bot_qq), no_cache=True)
            if isinstance(resp, dict) and resp.get("status") == "ok":
                card = resp.get("data", {}).get("card", "")
                match = re.search(r'[丨|｜]\s*(.+)', card)
                if match: return match.group(1).strip()
                return re.sub(r'^麦麦\s*', '', card).strip()
        except Exception: pass
        return ""

    # ==========================================
    # 管理员指令集
    # ==========================================
    def _is_admin(self, user_id: str) -> bool: return user_id == str(self.config.plugin.admin_qq)

    @Command("gallery_add", description="手动添加图库图片", pattern=r"(?<!\S)/?add_image\s+(?P<tags>.+?)\s*$")
    async def cmd_add_image(self, **kwargs):
        admin_qq = str(self.config.plugin.admin_qq)
        user_id = str(kwargs.get("user_id", ""))
        stream_id = str(kwargs.get("stream_id", ""))
        if not self._is_admin(user_id): return False, "权限不足", True

        tags_str = kwargs.get("matched_groups", {}).get("tags", "").strip()
        tags = [t.strip() for t in tags_str.split() if t.strip()]
        if not tags:
            await self.ctx.send.text("请指定标签，例如：/add_image 日落 风景", stream_id)
            return False, "标签为空", True

        # 记录状态，等待图片
        self.pending_upload[admin_qq] = {"tags": tags, "time": time.time()}
        await self.ctx.send.text(f"好的，请在 60 秒内把图片发给我，我会按【{', '.join(tags)}】入库。", stream_id)
        return True, "等待图片", True

    @Command("gallery_edit", description="修改图片标签", pattern=r"(?<!\S)/?edit_tag\s+(?P<img_hash>\w+)\s+(?P<tags>.+?)\s*$")
    async def cmd_edit_tag(self, **kwargs):
        user_id = str(kwargs.get("user_id", ""))
        stream_id = str(kwargs.get("stream_id", ""))
        if not self._is_admin(user_id): return False, "权限不足", True
        img_hash = kwargs.get("matched_groups", {}).get("img_hash", "")
        new_tags = [t.strip() for t in kwargs.get("matched_groups", {}).get("tags", "").strip().split() if t.strip()]
        if img_hash not in self.index.get("images", {}):
            await self.ctx.send.text(f"图库里找不到 ID 为 {img_hash} 的图片。", stream_id)
            return False, "图片不存在", True
        async with self.index_lock:
            self.index["images"][img_hash]["tags"] = new_tags
            self._save_index()
        await self.ctx.send.text(f"修改成功！图片 {img_hash} 的新标签为：【{', '.join(new_tags)}】", stream_id)
        return True, "修改成功", True

    @Command("gallery_del", description="删除某个标签下的所有图片", pattern=r"(?<!\S)/?del_image\s+(?P<tag>\S+)\s*$")
    async def cmd_del_image(self, **kwargs):
        user_id = str(kwargs.get("user_id", ""))
        stream_id = str(kwargs.get("stream_id", ""))
        if not self._is_admin(user_id): return False, "权限不足", True
        tag_to_del = kwargs.get("matched_groups", {}).get("tag", "").strip()
        deleted_hashes = []
        async with self.index_lock:
            for img_hash, img_info in list(self.index["images"].items()):
                if tag_to_del in img_info.get("tags", []):
                    file_path = self.image_dir / img_info["filename"]
                    if file_path.exists(): file_path.unlink()
                    del self.index["images"][img_hash]
                    deleted_hashes.append(img_hash)
            if deleted_hashes: self._save_index()
        if deleted_hashes:
            await self.ctx.send.text(f"已删除标签【{tag_to_del}】下的 {len(deleted_hashes)} 张图片。", stream_id)
        else:
            await self.ctx.send.text(f"图库中没有找到标签【{tag_to_del}】的图片。", stream_id)
        return True, "删除完毕", True

    @Command("gallery_list", description="查看图库状态", pattern=r"(?<!\S)/?gallery_list\s*$")
    async def cmd_gallery_list(self, **kwargs):
        user_id = str(kwargs.get("user_id", ""))
        stream_id = str(kwargs.get("stream_id", ""))
        if not self._is_admin(user_id): return False, "权限不足", True
        images = self.index.get("images", {})
        if not images:
            await self.ctx.send.text("图库现在是空的。", stream_id)
            return True, "空图库", True
        tag_counts = {}
        for img in images.values():
            for tag in img.get("tags", []): tag_counts[tag] = tag_counts.get(tag, 0) + 1
        msg = f"图库共 {len(images)} 张图，标签统计如下：\n"
        for tag, count in tag_counts.items(): msg += f"- {tag}: {count}张\n"
        await self.ctx.send.text(msg, stream_id)
        return True, "查询成功", True


def create_plugin() -> ImageGalleryPlugin:
    return ImageGalleryPlugin()